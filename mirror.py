#!/usr/bin/env python3
"""Build a NVDA Add-on Store mirror from multiple upstream catalogs.

Sources:
- https://github.com/nvaccess/addon-datastore (official NV Access catalog)
- https://bestmidi.com/addons/addons.json  (GitHub-discovered "bleeding edge" list)
- https://nvda-addons.ru/get.php?addonslist (Russian community catalog, many
  non-GitHub add-ons; the same JSON the TiendaNVDA/Store add-ons consume)
- https://nvda.es/files/get.php?addonslist (Spanish community catalog, with
  nvda-addons.org as its byte-identical failover; originals only)
- configured GitHub owners (validated direct `.nvda-addon` release assets)

Fetch -> filter (reject "rejected candidates") -> download + sha256 -> transform
to the NVDA add-on store schema -> emit a static site consumable by NVDA's
Add-on Store client (see NVDA source/addonStore/{network,dataManager}.py).

Stdlib only (Python 3.11+).
"""

import argparse
import concurrent.futures
import copy
import fnmatch
import glob
import hashlib
import io
import json
import os
import re
import threading
import time
import zipfile
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit, urlunsplit

BESTMIDI_URL = "https://bestmidi.com/addons/addons.json"
RU_ADDONS_URL = "https://nvda-addons.ru/get.php?addonslist"
ES_ADDONS_URLS = [
    "https://nvda.es/files/get.php?addonslist",
    "https://nvda-addons.org/files/get.php?addonslist",
]
NVDA_BUILD_VERSION_URL = (
    "https://raw.githubusercontent.com/nvaccess/nvda/master/source/buildVersion.py"
)
NVDA_API_VERSION_URL = (
    "https://raw.githubusercontent.com/nvaccess/nvda/master/source/addonAPIVersion.py"
)

#: Fallback BACK_COMPAT_TO (year, major, minor). NVDA considers an add-on
#: "compatible" when minimumNVDAVersion <= current and
#: lastTestedNVDAVersion >= BACK_COMPAT_TO. Kept in sync with
#: nvaccess/nvda source/addonAPIVersion.py, but refreshed from that file at
#: build time when reachable. NVDA raises this on the first release of each
#: year, so a stale value here silently publishes add-ons the new release
#: rejects; back_compat_to_version() logs whenever it has to fall back.
FALLBACK_BACK_COMPAT_TO = (2027, 1, 0)

# Every locale NVDA ships (source/locale/*). NVDA requests its store data at
# {base}/{lang}/{channel}/{apiVersion}.json and uses the language code only as a
# cache key -- the returned list is identical for every language.
LOCALES = [
    "af_ZA", "am", "an", "ar", "as", "be", "bg", "bn", "bs", "ca", "ckb", "cs",
    "da", "de", "de_CH", "el", "en", "es", "es_CO", "fa", "fi", "fr", "ga", "gl",
    "gu", "he", "hi", "hr", "hu", "id", "is", "it", "ja", "ka", "km", "kmr", "kn",
    "ko", "kok", "ky", "lb", "lt", "mk", "ml", "mn", "mni", "my", "nb_NO", "ne",
    "nl", "nn_NO", "pa", "pl", "pt_BR", "pt_PT", "ro", "ru", "sk", "sl", "so",
    "sq", "sr", "sv", "ta", "te", "th", "tr", "uk", "ur", "vi", "zh_CN", "zh_HK",
    "zh_TW",
]

# NVDA always requests channel "all" (its _preferredChannel is fixed) and
# filters stable/beta/dev client-side, so only "all" is emitted. That keeps the
# published site small enough for GitHub Pages, which also forbids symlinks --
# so every path below is a real copy.
CHANNELS = ["all"]

# API versions for NVDA releases still in active use. NVDA requests
# {base}/{lang}/all/{year}.{major}.{minor}.json using its OWN add-on API version
# (see NVDA source/addonStore/network.py _getCurrentApiVersionForURL), so every
# released NVDA version a user might still run needs a file here or they get a
# 404 and an empty "compatible" list. "latest" always resolves the "show all
# (incompatible)" view; the numbered entries cover the default "compatible"
# view. Every Add-on Store-era API version, including experimental entries, is
# selected from NV Access's live addon-datastore metadata at build time. The
# current dev version is also prepended from NVDA master when needed.
#
# NVDA 2025.1 is the floor. The Add-on Store client shipped earlier -- in
# 2023.2 -- and 2023.2 through 2024.4 do request
# {lang}/{channel}/{apiVersion}.json, but from a hardcoded address:
# addonStore.network.BASE_URL = "https://nvaccess.org/addonStore", with no
# setting to change it. The [addonStore] baseServerURL key this mirror needs
# was added in 2025.1, where _getBaseURL() first consults it. Files for older
# API versions are therefore unreachable by every NVDA ever released, and
# publishing them cost roughly a third of the deployed site. All 2025.1+
# versions remain published permanently as new versions are appended.
ADDON_STORE_FIRST_API_VERSION = (2025, 1, 0)

#: How many NVDA release years to publish in full. Each release line
#: (year.major) costs one filtered copy of the catalog per locale -- about
#: 120 MB across the 74 locales -- and NVDA ships roughly three lines a year,
#: so publishing every line since 2025.1 outgrows what GitHub Pages will hold.
#:
#: Retention follows where users actually are. The current and previous years
#: keep every line, because that is where nearly everyone sits and a missing
#: file is an empty Add-on Store, not a degraded one -- NVDA has no version
#: fallback. Older years keep only their newest line, on the assumption that
#: anyone that far behind who is still updating at all has taken their year's
#: last patch. Superseded patches within a line are always dropped: NVDA moves
#: those users forward on its own, and every patch in a line shares one
#: BACK_COMPAT_TO, so they are near-duplicate files nobody requests.
API_VERSION_YEARS_IN_FULL = 2

#: Warn when the built site passes this. GitHub Pages documents 1 GB as a soft
#: limit; going over is not refused outright but does invite throttling and a
#: warning mail, so the build says so rather than letting it be a surprise.
SITE_SIZE_WARN_BYTES = 1_000_000_000

# API version regex mirrors NVDA source/addonAPIVersion.py: year.major(.minor)
_API_VERSION_RE = re.compile(r"^(0|\d{4})\.(\d)(?:\.(\d))?$")

# NVDA master declares these as annotated, module-level assignments:
#     BACK_COMPAT_TO: AddonApiVersionT = (2027, 1, 0)
#     version_year = 2027
# Both are anchored to the start of a line so an incidental mention earlier in
# the file (a docstring, an f-string, the BACK_COMPAT_TO changelog block) can
# never be picked up ahead of the real declaration.
_MASTER_BACK_COMPAT_TO_RE = re.compile(
    r"^BACK_COMPAT_TO\s*(?::[^=\n]*)?=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
    re.MULTILINE,
)


def _master_build_version_re(name):
    return re.compile(rf"^{name}\s*(?::[^=\n]*)?=\s*(\d+)", re.MULTILINE)


_INT_RUN_RE = re.compile(r"\d+")

#: Cyrillic block, used to detect Russian (nvda-addons.ru) text so the store
#: can prefer English where an English sibling source exists.
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_NON_LATIN_SCRIPT_RE = re.compile(
    r"[\u0370-\u06ff\u0900-\u0e7f\u10a0-\u10ff"
    r"\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]"
)
#: Words that mark a changelog as written in a language other than English.
#: Every alternative must be a word English prose never uses. Unaccented
#: "version" and the "correc..." family are deliberately absent: they matched
#: ordinary English release notes ("Version 1.2", "corrected a crash") and
#: replaced them with the not-available placeholder, hiding the real notes of
#: hundreds of add-ons that already published them in English.
_NON_ENGLISH_LATIN_CHANGELOG_RE = re.compile(
    r"(?i)\b(?:versión(?:es)?|a[ñn]adid[ao]s?|novedades|"
    r"correcci[oó]n(?:es)?|corregid[ao]s?|corre[çc]{1,2}[aã]o|corre[çc]{1,2}[oõ]es|"
    r"melhorias|vers[aã]o|adicionad[ao]|mudan[cç]as|"
    r"am[ée]lioration(?:s)?|am[ée]lior[ée]e?s?|"
    r"ajout[eé]e?s?|s[uü]r[uü]m|eklendi|d[uü]zeltildi|ditambahkan|"
    r"perbaikan)\b"
)
_KNOWN_NON_ENGLISH_CHANGELOG_IDS = {
    "ChromeUtilities", "IslamicPedia", "Open_Bible", "Progress Reader",
    "RemapKeyAplication", "TelegramJusti", "brailabEmulated",
    "calendario_simples_BR", "emoticonosAvanzados", "invisinote",
    "referenceToneTuner", "scintillaIMECaretFix",
    "sonidos_navegacion_ruben", "steelSeriesBattery", "tdkSozluk",
    "textToAudioConverter", "virtualBrailleDisplay", "vozNativaDoDosvox",
    "wordAccessibility", "zRadio",
}

_TEMPLATE_NAMES = {"addontemplate", "__addon_id__"}

USER_AGENT = (
    "Mozilla/5.0 (compatible; nvda-addon-mirror/1.0; +https://github.com/"
    "serrebidev/nvda-addon-mirror)"
)

ALL_SOURCES = ("official", "bestmidi", "ru", "es", "github_owner", "pinned")

#: How much each source is trusted when two of them describe the same add-on.
#: Used both to pick the winning entry (see dedupe) and to pick which source's
#: English release notes to borrow (see english_changelogs).
SOURCE_PRIORITY = {
    "pinned": 5,
    "github_owner": 4,
    "official": 3,
    "ru": 2,
    "bestmidi": 1,
    "es": 0,
}
PINNED_CONFIG_PATH = "pinned.json"
GITHUB_OWNERS_PATH = "githubOwners.json"
GITHUB_OWNER_CACHE_PATH = "githubOwnerCache.json"
#: Bump when a change alters how a cached entry is derived from a bundle. The
#: cache is keyed by asset identity, so an entry stays forever while the asset
#: is untouched -- which left add-ons published under a description that their
#: manifest had never named them. Changing this re-derives every entry once.
GITHUB_OWNER_CACHE_VERSION = "v2-displayname"
GITHUB_OWNER_DISCOVERY_TTL_SECONDS = 24 * 60 * 60
DOWNLOAD_RECHECK_SECONDS = 24 * 60 * 60
DOWNLOAD_RETRY_SECONDS = 6 * 60 * 60
#: A catalog that publishes a free-form version ("unknown", "current") still
#: ships the real one inside the bundle's manifest.ini. The hashing pass
#: already streams those bytes, so the manifest is read from the stream it
#: keeps rather than from a second request. Bundles above this size are hashed
#: without buffering: no ordinary add-on is that large, and the packs that are
#: (voice and speech data) are not published anyway.
MANIFEST_CAPTURE_LIMIT = 32 * 1024 * 1024
PINNED_BUNDLE_CACHE_PATH = ".bundlecache"

# Newly discovered repositories cost one uncached request each, while repeat
# checks are conditional and free. Adding an author with hundreds of repos would
# otherwise spend the whole hourly budget in a single run, so first-time checks
# are rationed and the remainder is carried in the cache for the next run.
GITHUB_NEW_REPOSITORY_BUDGET = 120
NVDA_API_VERSIONS_PATH = "nvdaAPIVersions.json"
NVDA_API_VERSIONS_URL = (
    "https://raw.githubusercontent.com/nvaccess/addon-datastore/"
    "master/transform/nvdaAPIVersions.json"
)

# Human-readable upstream names carried into the combined catalog. NVDA ignores
# unknown JSON fields, while the helper add-on uses ``storeSource`` to expose
# this provenance in the Add-on Store list.
STORE_SOURCE_LABELS = {
    "official": "NV Access Add-on Store",
    "pinned": "Pinned GitHub release",
    "github_owner": "GitHub author release",
    "ru": "NVDA Add-ons RU",
    "bestmidi": "BestMidi",
    "es": "NVDA.es",
}
#: A pinned entry is one source for priority and dedupe, but it reaches the
#: mirror two ways. This labels the half that came from an author's own site,
#: so the helper add-on does not describe it as a GitHub release. Deliberately
#: not a STORE_SOURCE_LABELS key: it is a provenance label, not a source.
PINNED_URL_SOURCE_LABEL = "Author's website"
GITHUB_API = "https://api.github.com"
GITHUB_OWNER_REJECTIONS = []

# GitHub API token, when present (e.g. GITHUB_TOKEN in Actions). Raises the
# api.github.com rate limit from 60 to 1000 requests/hour, which matters when
# the mirror rebuilds more often than the upstream catalogs change.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def log(msg):
    print(msg, flush=True)


def quote_url(url):
    """Percent-encode the path of a URL, tolerating spaces and other
    characters that urllib rejects. Keeps scheme, host, query and fragment."""
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def http_get(url, timeout=120, headers=None):
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = Request(url, headers=h)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_json(url, timeout=120, headers=None):
    return json.loads(http_get(url, timeout=timeout, headers=headers).decode("utf-8-sig"))


def http_get_conditional(url, validators=None, timeout=120):
    """GET url, returning (body, etag, last_modified).

    Sends the caller's stored validators so an unchanged resource comes back
    as a 304 (raised as HTTPError, for the caller to treat as "reuse what you
    have") instead of a full body. The mirror re-reads the same large catalogs
    every hour, so this is the difference between a few hundred bytes and
    megabytes per language per build.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if validators:
        if validators.get("etag"):
            headers["If-None-Match"] = validators["etag"]
        elif validators.get("last_modified"):
            headers["If-Modified-Since"] = validators["last_modified"]
    req = Request(quote_url(url), headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return (
            resp.read(),
            resp.headers.get("ETag"),
            resp.headers.get("Last-Modified"),
        )


def sanitize_version(version):
    """Return (major, minor, patch) ints, or None.

    Lenient on purpose: many non-GitHub add-ons (e.g. RHVoice voice packs) use
    versions like "4.1.1009.12", "2023.12.10.06.44.50", "v20" or "1.0-beta".
    We keep the first up-to-3 integer runs and pad with 0, so they map onto
    NVDA's MajorMinorPatch (which requires 2-3 integer parts).
    """
    if not version:
        return None
    runs = _INT_RUN_RE.findall(version)
    if not runs:
        return None
    nums = [int(r) for r in runs[:3]]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _release_tag_version(tag):
    """Parse a version-shaped GitHub tag without treating incidental digits as versions."""
    if not tag:
        return None
    match = re.fullmatch(
        r"(?i)v?[-_.]?(\d+(?:[._-]\d+){0,3})"
        r"(?:[-_.]?(?:alpha|beta|b|rc|dev|rs)\d*)?",
        tag.strip(),
    )
    return sanitize_version(match.group(1)) if match else None


def parse_api_version(version):
    """Return (major, minor, patch) ints or None, using NVDA's API regex."""
    if not version:
        return None
    m = _API_VERSION_RE.match(version.strip())
    if not m:
        return None
    year, major, minor = m.groups()
    return (int(year), int(major), int(minor) if minor is not None else 0)


def parse_iso8601_to_ms(value):
    """Parse an ISO-8601 timestamp to epoch milliseconds, or None."""
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def parse_http_date_to_ms(value):
    """Parse an HTTP Last-Modified header to epoch milliseconds, or None."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def parse_ru_modified(value):
    """Parse nvda-addons.ru "2026-08-26 22:21:05" (Moscow, UTC+3) to epoch ms."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
    return int(dt.timestamp() * 1000)


def clean_text(text):
    """Strip docstring quote wrappers and whitespace some catalogs leave."""
    if not text:
        return ""
    t = text.strip()
    if t.startswith('"""'):
        t = t[3:].lstrip()
    if t.endswith('"""'):
        t = t[:-3].rstrip()
    if t.startswith("'''"):
        t = t[3:].lstrip()
    if t.endswith("'''"):
        t = t[:-3].rstrip()
    return t.strip()


def sha256_stream(url, timeout=120, validators=None, capture_limit=0):
    """Stream-download url and return its digest, size, HTTP validators, body.

    ``capture_limit`` keeps the streamed bytes in memory up to that many bytes
    so a caller can read the bundle's manifest without asking the host for the
    file a second time. The body is None when nothing was requested or the
    download grew past the limit; the digest and size are unaffected either
    way.
    """
    log(f"Package request: {url}")
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if validators:
        if validators.get("etag"):
            headers["If-None-Match"] = validators["etag"]
        elif validators.get("last_modified"):
            headers["If-Modified-Since"] = validators["last_modified"]
    req = Request(quote_url(url), headers=headers)
    digest = hashlib.sha256()
    size = 0
    buffered = [] if capture_limit else None
    with urlopen(req, timeout=timeout) as resp:
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if buffered is not None:
                if size > capture_limit:
                    buffered = None
                else:
                    buffered.append(chunk)
    body = b"".join(buffered) if buffered is not None else None
    return digest.hexdigest(), size, etag, last_modified, body


def bundle_locale_translations(raw):
    """Return {lang: {"displayName":..., "description":...}} from a bundle.

    NVDA add-ons carry their own translations at locale/<lang>/manifest.ini,
    which is where addonHandler reads a translated summary and description
    from. Those are the author's own words, so they beat anything this build
    could generate.

    Harvested from bytes that were already being streamed for the sha256, so
    it costs no extra request. Returns {} for anything unreadable: a damaged
    download simply leaves the add-on in English.
    """
    if not raw:
        return {}
    found = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for name in archive.namelist():
                parts = name.split("/")
                if (len(parts) != 3 or parts[0] != "locale"
                        or parts[2] != "manifest.ini"):
                    continue
                lang = parts[1].strip()
                if not lang or lang == "en":
                    continue
                try:
                    text = archive.read(name).decode("utf-8-sig", "replace")
                except (KeyError, OSError, ValueError):
                    continue
                # A translated manifest carries summary/description only; the
                # store's displayName is the manifest's summary.
                entry = {}
                summary = clean_text(_manifest_value(text, "summary"))
                description = clean_text(_manifest_value(text, "description"))
                if summary:
                    entry["displayName"] = summary
                if description:
                    entry["description"] = description
                if entry:
                    found[lang] = entry
    except (zipfile.BadZipFile, OSError, ValueError):
        return {}
    return found


def bundle_manifest_summary(raw):
    """Return the name declared in a bundle's manifest.ini, or "".

    manifest.ini's ``summary`` is what NVDA itself shows as the add-on's name,
    so it is the authority when a catalog states a description instead.
    """
    if not raw:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            text = archive.read("manifest.ini").decode("utf-8-sig", "replace")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return ""
    return clean_text(_manifest_value(text, "summary"))


def bundle_manifest_version(raw):
    """Return the version declared in a downloaded bundle's manifest.ini.

    Returns "" for anything that is not a readable add-on bundle: the caller
    is recovering a version a catalog failed to state, so a damaged or
    unexpected download simply leaves the catalog's own string in place.
    """
    if not raw:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest_text = archive.read("manifest.ini").decode("utf-8-sig", "replace")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return ""
    return _manifest_value(manifest_text, "version").strip()


def cached_download(entry, cached=None, force=False, now=None, capture_limit=0,
                    inspect=False):
    """Return (cache record, error), with no package request between daily checks.

    A changed catalog version bypasses the TTL. Conditional GET handles both
    unchanged files (304) and replacements in one request, even on hosts without
    HEAD/range support. Failures are backed off, including first-time failures.

    With ``capture_limit`` set, the streamed bytes are held so the version
    declared inside the bundle and the author's own translations can be read
    out of them. Both are cached alongside the digest, so they cost one
    download per release rather than one per build.

    ``inspect`` additionally forces one download when the cache has no answer
    yet. That is only ever set for a version the catalogs failed to state,
    where the add-on is otherwise unpublishable. Everything else waits for the
    ordinary recheck schedule: turning a new capture into a forced refresh
    would re-download the entire catalog in a single build.
    """
    now = time.time() if now is None else now
    cached = cached if isinstance(cached, dict) else {}
    version = entry.get("version")
    same_version = cached.get("version") == version
    usable = bool(cached.get("sha256") and cached.get("size") is not None)
    # A cache written before the version was ever looked for has no answer to
    # reuse. Inspect the bundle once, then fall back to the ordinary schedule.
    # A URL whose last fetch failed keeps its retry backoff: recovering a
    # version is never a reason to hammer a host that is already refusing.
    unexamined = (
        inspect
        and bool(capture_limit)
        and "manifest_version" not in cached
        and not cached.get("error")
    )
    if not force and not unexamined:
        if same_version and cached.get("next_check", 0) > now:
            return dict(cached), cached.get("error")
        # Seed old deployments without probing every author's files at once.
        if usable and "next_check" not in cached and (
            same_version or cached.get("version") is None
        ):
            return dict(cached, version=version,
                        next_check=now + DOWNLOAD_RECHECK_SECONDS), None
    try:
        digest, size, etag, modified, body = sha256_stream(
            entry["download_url"],
            # An unexamined bundle must arrive as a body, so it is asked for
            # unconditionally; a 304 would carry no manifest to read.
            validators=(
                cached
                if usable and same_version and not force and not unexamined
                else None
            ),
            capture_limit=capture_limit,
        )
    except HTTPError as exc:
        status = exc.code
        error = str(exc)
        exc.close()
        if status == 304 and usable and same_version and not force:
            refreshed = dict(cached, next_check=now + DOWNLOAD_RECHECK_SECONDS)
            refreshed.pop("error", None)
            if capture_limit:
                refreshed.setdefault("manifest_version", "")
            return refreshed, None
    except (URLError, OSError) as exc:
        error = str(exc)
    else:
        record = {"sha256": digest, "size": size, "etag": etag,
                  "last_modified": modified, "version": version,
                  "next_check": now + DOWNLOAD_RECHECK_SECONDS}
        if capture_limit:
            # Store the answer even when it is empty: the key's presence is how
            # the next build knows this bundle has already been inspected and
            # must not be fetched again just to look for a version.
            record["manifest_version"] = bundle_manifest_version(body)
            # The author's own translations, taken from bytes already in hand.
            # Deliberately NOT part of the "unexamined" force-download rule:
            # a bundle is inspected for these only when it was being fetched
            # anyway, so enabling the harvest never re-downloads the whole
            # catalog at once. Coverage fills in over a day as the normal
            # recheck TTLs expire.
            record["manifest_locales"] = bundle_locale_translations(body)
            # The add-on's own name, for catalogs whose summary is a
            # description. Same free bytes as the two above.
            record["manifest_summary"] = bundle_manifest_summary(body)
        return record, None
    # Keep previous validators only for the same catalog version. Do not publish
    # a stale hash for a changed version or a URL whose refresh failed.
    failed = dict(cached) if same_version else {}
    failed.update(version=version, error=error,
                  next_check=now + DOWNLOAD_RETRY_SECONDS)
    return failed, error


# ---------------------------------------------------------------------------
# Source fetchers: each returns a list of normalized entry dicts with the keys
# below. A shared transform() then turns them into NVDA store objects.
#
#   name, summary, description, author, version, channel, homepage,
#   source_url, license, license_url, changelog, download_url, submission_ms,
#   min_nvda (tuple|None), last_tested (tuple|None), source
# ---------------------------------------------------------------------------

def _norm_channel_bestmidi(channel):
    c = (channel or "").strip().lower()
    if c in ("stable", "beta", "dev", "external"):
        return c
    return "stable"


#: alpha/beta/rc/dev/pre markers inside a version string, which is the only
#: corroboration available for a pre-release label at fetch time.
#: The marker may sit straight against a digit ("0.5dev", "1.0.0alpha2"), so
#: only a letter on either side rules it out -- that is what keeps "dev" inside
#: "development" or a name like "Ardev" from counting as a pre-release.
_PRERELEASE_VERSION_RE = re.compile(
    r"(?i)(?:^|[^a-z])(?:alpha|beta|rc|preview|pre|dev|snapshot|nightly)"
    r"(?:[^a-z]|$)"
)


def version_marks_prerelease(version):
    """True when a version string says for itself that it is a pre-release."""
    return bool(_PRERELEASE_VERSION_RE.search((version or "").strip()))


def _norm_channel_ru(channel, version=None):
    """Normalise an nvda-addons.ru channel label, which is mostly noise.

    That feed marks 691 of its 775 links "Dev", including ordinary releases
    like Acapela TTS Voices 1.9.5 and 4shared 1.0 -- 89% of the catalog, of
    which only 29 carry any pre-release marker in their own version string.
    Taken at face value it hides several hundred perfectly normal add-ons from
    the Stable view that NVDA's Add-on Store shows by default, which is where
    almost everyone looks.

    So a pre-release label is honoured only when the version string corroborates
    it. An uncorroborated "Dev" says nothing and is treated as stable, which is
    also what NVDA assumes for an add-on that declares no channel at all.
    """
    c = (channel or "").strip().lower()
    if c not in ("dev", "alpha", "beta", "rcbeta", "stable"):
        return "stable"
    if c == "stable":
        return "stable"
    if version is not None and not version_marks_prerelease(version):
        return "stable"
    if c in ("dev", "alpha"):
        return "dev"
    return "beta"


def _norm_channel_es(channel):
    c = (channel or "").strip().lower()
    if c in ("dev", "alpha"):
        return "dev"
    if c in ("beta", "rc", "rcbeta"):
        return "beta"
    # The feed also contains an "old" channel. It is a legacy download rather
    # than a channel understood by NVDA's Add-on Store, so callers skip it.
    return "stable"


# The official NV Access store and the NVDA Chinese community mirror
# (addonstore.nvaccess.mirror.nvdadr.com) serve the SAME catalog data; the
# Chinese site is a CDN mirror used as a failover if the official one is slow.
OFFICIAL_STORE_URLS = [
    "https://addonstore.nvaccess.org/en/all/latest.json",
    "https://addonstore.nvaccess.mirror.nvdadr.com/en/all/latest.json",
]

#: The official store publishes a separate view per language, and for add-ons
#: whose authors supplied translations the displayName and description in it
#: are genuinely translated (107 of 528 French descriptions differ from the
#: English ones). That is the best translation source available: written by the
#: add-on's own author, already reviewed by NV Access, and free.
OFFICIAL_STORE_LOCALE_URLS = [
    "https://addonstore.nvaccess.org/{lang}/all/latest.json",
    "https://addonstore.nvaccess.mirror.nvdadr.com/{lang}/all/latest.json",
]

#: Persisted between builds and republished with the site, like hashcache.json.
#: Holds each locale's ETag plus only the strings that actually differ from
#: English, so an unchanged language costs one 304 rather than 1.7 MB.
LOCALE_CACHE_PATH = "localeCache.json"

#: Normalized entries from the catalog sources that are not GitHub releases,
#: with the poll that produced them. These catalogs change less often than an
#: hourly build, and none of them can be checked cheaply: the NV
#: Access store answers a conditional request with the whole 9.5 MB body
#: whenever its CDN last refilled, and nvda-addons.ru, nvda.es and bestmidi
#: send no ETag and no Last-Modified at all. Re-fetching them every build would
#: pull ~15 MB per run from four hosts, which is what SOURCE_POLL_SECONDS
#: exists to prevent; between polls the previous entries are reused verbatim.
SOURCE_CACHE_PATH = "sourceCache.json"

#: How long a polled catalog stays fresh. One hour is the cadence these sources
#: were already being read at, held steady while the GitHub-release sources --
#: which cost nothing but a conditional request -- refresh with every build.
#: The NV Access store asks for four hours itself (Cache-Control: max-age=14400)
#: so this is already well inside anything the upstreams request.
SOURCE_POLL_SECONDS = int(os.environ.get("SOURCE_POLL_SECONDS", str(60 * 60)))

#: A failed poll reuses the last good entries rather than dropping a whole
#: catalog, and waits this long before trying the host again. Short enough to
#: recover within an hour, long enough that a host having a bad day is not
#: retried by every build.
SOURCE_RETRY_SECONDS = int(os.environ.get("SOURCE_RETRY_SECONDS", str(10 * 60)))

#: Fields the store shows as prose, in the order a reader meets them.
TRANSLATABLE_FIELDS = ("displayName", "description")

#: Machine translation is the last resort, behind the add-on author's own
#: words. It is off unless OPENROUTER_API_KEY is set, so the build works
#: unchanged with no credentials and simply leaves those entries in English.
#: One provider and one key for both directions: this translates English into
#: each locale, and auto_translate.py brings non-English metadata back into
#: English.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TRANSLATE_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
TRANSLATE_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-3.8-flash")

#: Reasoning cannot be disabled on this model, but "low" spends zero reasoning
#: tokens and answers identically for translation -- measured 5.5x cheaper than
#: "medium", at roughly $0.0001 per string once batched.
TRANSLATE_REASONING_EFFORT = "low"

#: Strings per request. Batching amortises the prompt across many translations,
#: which is where nearly all of the saving comes from.
TRANSLATE_BATCH_SIZE = 20

#: Characters of source text a single build may send. The first full pass over
#: 73 locales is tens of millions of characters, so an uncapped build could
#: spend a lot in one run before anyone noticed. Work is spread across builds
#: instead: whatever is not translated this hour stays English and is picked up
#: next hour.
TRANSLATE_CHAR_BUDGET = int(os.environ.get("TRANSLATE_CHAR_BUDGET", "200000"))

#: Republished with the site and restored next build, like hashcache.json.
#: Keyed by (sha256 of the source text, target language), so text that has not
#: changed is never paid for twice.
TRANSLATION_CACHE_PATH = "translationCache.json"

#: The official store's per-language views are swept for author-written
#: translations. That is 73 requests to one host, and a language gains a
#: translation about as often as an add-on is published, not every five
#: minutes. Between sweeps the cached strings are reused; the ETags in
#: LOCALE_CACHE_PATH still make each sweep itself nearly free.
LOCALE_POLL_SECONDS = int(os.environ.get("LOCALE_POLL_SECONDS", str(6 * 60 * 60)))

#: Where the sweep's own timestamp lives inside the locale cache. Not a
#: language, and deliberately not shaped like one, because every other key in
#: that file is a locale code.
LOCALE_POLL_KEY = "__poll__"

def _translation_row(addon):
    """Key a translation the way the catalog itself is keyed.

    The official store lists one row PER CHANNEL for the same add-on (a stable
    and a dev row of robEnhancements are separate entries with their own text),
    and NVDA indexes by [channel][addonId]. Keying translations by addonId
    alone silently lets one channel's wording overwrite the other's.
    """
    addon_id = (addon.get("addonId") or addon.get("name") or "").strip()
    channel = (addon.get("channel") or "stable").strip().lower()
    # Joined on a tab: no addonId or channel contains one, and the result is a
    # plain string, so these maps serialise straight into the JSON caches.
    return f"{addon_id}	{channel}" if addon_id else ""


def _english_baseline(entries):
    """Map (addonId, channel) -> English strings, for spotting translations."""
    baseline = {}
    for a in entries:
        row = _translation_row(a)
        if row:
            baseline[row] = {
                field: a.get(field) or "" for field in TRANSLATABLE_FIELDS
            }
    return baseline


def fetch_official_english_baseline(cached=None):
    """Return (baseline, cache record) from the official store's own English view.

    The comparison that decides "is this string actually translated?" has to
    be against the *store's* English, not the mirror's. The mirror rewrites
    some English text of its own -- borrowing an English description for a
    Russian-only add-on, substituting a changelog placeholder -- so comparing
    a language against the mirror's English marks untranslated add-ons as
    translated and then publishes English under a Japanese URL.
    """
    cached = cached if isinstance(cached, dict) else {}
    validators = {
        "etag": cached.get("etag"),
        "last_modified": cached.get("last_modified"),
    }
    for template in OFFICIAL_STORE_LOCALE_URLS:
        url = template.format(lang="en")
        try:
            raw, etag, last_modified = http_get_conditional(url, validators)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 304 and "baseline" in cached:
                return cached["baseline"], dict(cached)
            continue
        except (URLError, OSError):
            continue
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        baseline = _english_baseline(data)
        return baseline, {
            "etag": etag,
            "last_modified": last_modified,
            "baseline": baseline,
        }
    return cached.get("baseline", {}), dict(cached)


def fetch_official_locale_translations(lang, baseline, cached=None):
    """Return (translations, cache record) for one official-store language.

    Only strings that differ from English are kept: for most languages that is
    a small minority of the catalog, and storing the rest would make the cache
    as large as the catalog times seventy-four for no information.

    A 304 means the language is unchanged, so the previous translations are
    reused without re-parsing 1.7 MB of JSON. Any failure returns whatever was
    cached rather than dropping the language back to English mid-build.
    """
    cached = cached if isinstance(cached, dict) else {}
    validators = {
        "etag": cached.get("etag"),
        "last_modified": cached.get("last_modified"),
    }
    for template in OFFICIAL_STORE_LOCALE_URLS:
        url = template.format(lang=lang)
        try:
            raw, etag, last_modified = http_get_conditional(url, validators)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 304 and "translations" in cached:
                return cached["translations"], dict(cached)
            continue
        except (URLError, OSError):
            continue
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        translations = {}
        for a in data:
            row = _translation_row(a)
            english = baseline.get(row)
            if not row or english is None:
                continue
            translated = {}
            for field in TRANSLATABLE_FIELDS:
                value = a.get(field) or ""
                if value and value != english.get(field):
                    translated[field] = value
            if translated:
                # JSON keys must be strings; rejoin on a character no addonId
                # or channel contains.
                translations[row] = translated
        return translations, {
            "etag": etag,
            "last_modified": last_modified,
            "translations": translations,
        }
    # Every mirror failed. Keep the last good answer for this language.
    return cached.get("translations", {}), dict(cached)


def translation_key(text, lang):
    """Cache key for one string in one language."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return f"{lang}:{digest}"


def machine_translation_target(lang):
    """Language name to translate into, or None when the locale is unusable.

    Every NVDA locale is served: a general model needs a language, not a code
    from a provider's supported list, so there is no subset to fall outside of.
    """
    code = (lang or "").strip()
    if not code or code == "en" or code.startswith("en_"):
        return None
    return code


def machine_translate_batch(texts, target, timeout=180):
    """Translate a batch of strings, or return None if the call failed.

    The return is positional: index i of the result is the translation of index
    i of ``texts``. Anything that does not come back with exactly one answer per
    input is discarded whole, because a short or reordered reply would attach
    one add-on's text to another add-on's name.
    """
    if not texts or not TRANSLATE_API_KEY:
        return None
    keyed = {str(i): text for i, text in enumerate(texts)}
    payload = json.dumps({
        "model": TRANSLATE_MODEL,
        "reasoning": {"effort": TRANSLATE_REASONING_EFFORT},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "You translate NVDA screen-reader add-on metadata from English "
                f"into the language with code '{target}'.\n"
                "- Reply with the translation only, no commentary.\n"
                "- NEVER translate a product name, a brand, a key name such as "
                "NVDA+F12, a file extension, or a URL. Leave them exactly.\n"
                "- Keep the original line breaks.\n"
                "Reply with ONLY a JSON object mapping each input key to its "
                "translation."
            )},
            {"role": "user", "content": json.dumps(keyed, ensure_ascii=False)},
        ],
    }).encode("utf-8")
    request = Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {TRANSLATE_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        log(f"machine translation failed for {target}: {exc}")
        return None
    if body.get("error"):
        log(f"machine translation failed for {target}: {str(body['error'])[:160]}")
        return None
    try:
        answers = json.loads(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        log(f"machine translation returned an unreadable answer ({target}): {exc}")
        return None
    if not isinstance(answers, dict) or len(answers) != len(keyed):
        log(f"machine translation returned {len(answers or [])} results for "
            f"{len(keyed)} strings ({target}); discarding the batch")
        return None
    result = []
    for index in range(len(texts)):
        value = answers.get(str(index))
        result.append(value.strip() if isinstance(value, str) and value.strip()
                      else None)
    return result


def machine_translate_missing(needed, cache, budget=TRANSLATE_CHAR_BUDGET):
    """Fill translation gaps from the provider, within one build's budget.

    ``needed`` is an iterable of (lang, text). Returns the number of characters
    actually sent. The cache is updated in place, including negative results
    (recorded as an empty string) so a string the provider cannot handle is not
    retried every hour forever.
    """
    if not TRANSLATE_API_KEY:
        return 0
    by_lang = {}
    for lang, text in needed:
        if not text or translation_key(text, lang) in cache:
            continue
        by_lang.setdefault(lang, []).append(text)

    spent = 0
    # Fewest gaps first, so small languages reach full coverage rather than
    # every language staying perpetually half-translated.
    for lang in sorted(by_lang, key=lambda k: len(by_lang[k])):
        target = machine_translation_target(lang)
        if target is None:
            continue
        batch, batch_chars = [], 0
        pending = list(dict.fromkeys(by_lang[lang]))
        while pending or batch:
            if pending and len(batch) < 40 and batch_chars + len(pending[0]) <= 25000:
                text = pending.pop(0)
                if spent + batch_chars + len(text) > budget:
                    pending.clear()
                    if not batch:
                        break
                else:
                    batch.append(text)
                    batch_chars += len(text)
                    continue
            if not batch:
                break
            results = machine_translate_batch(batch, target)
            spent += batch_chars
            for source, translated in zip(batch, results or [None] * len(batch)):
                cache[translation_key(source, lang)] = translated or ""
            batch, batch_chars = [], 0
            if results is None:
                # The provider is unhappy; stop pestering it this build.
                break
        if spent >= budget:
            log(f"machine translation budget of {budget} characters reached; "
                f"remaining gaps stay English until the next build")
            break
    return spent


def fetch_official():
    """Fetch the NV Access store catalog (with Chinese mirror as failover).

    These add-ons already carry sha256 and VirusTotal data, so they cost zero
    download time. The JSON is passed through nearly verbatim -- it already
    follows NVDA's expected schema -- and normalized into our entry shape so
    dedupe/transform see one consistent format.
    """
    data = None
    for url in OFFICIAL_STORE_URLS:
        try:
            data = http_get_json(url)
            break
        except (HTTPError, URLError, OSError) as exc:
            log(f"official store source {url} failed: {exc}")
    if data is None:
        raise RuntimeError("could not fetch any official store source")

    entries = []
    for a in data:
        scan = a.get("scanResults")
        entries.append(
            {
                "name": (a.get("addonId") or "").strip(),
                "summary": clean_text(a.get("displayName")),
                "description": a.get("description") or "",
                "author": (a.get("publisher") or "").strip(),
                "version": (a.get("addonVersionName") or "").strip(),
                "channel": (a.get("channel") or "stable").strip().lower(),
                "homepage": (a.get("homepage") or "").strip(),
                "source_url": (a.get("sourceURL") or "").strip(),
                "license": (a.get("license") or "").strip(),
                "license_url": (a.get("licenseURL") or "").strip(),
                "changelog": clean_text(a.get("changelog")),
                "download_url": (a.get("URL") or "").strip(),
                "submission_ms": a.get("submissionTime") or None,
                # min_nvda must come from minNVDAVersion (the minimum NVDA the
                # add-on supports), NOT addonVersionNumber (the add-on's own
                # release version). Using the latter made e.g. robEnhancements
                # claim minNVDA 2026.5.3 instead of 2024.1.0, breaking NVDA's
                # client-side compatibility gating.
                "min_nvda": parse_api_version_dict(a.get("minNVDAVersion") or {}),
                "last_tested": parse_api_version_dict(a.get("lastTestedVersion") or {}),
                # Pass-through: already computed upstream, so no download needed.
                "sha256": (a.get("sha256") or "").strip().lower(),
                # Sanitized scan data: drop explicit-null dicts so NVDA's
                # fromDict neither errors nor floods the log.
                "scan_results": _sanitize_scan(scan, a),
                "source": "official",
            }
        )
    return entries


def parse_api_version_dict(d):
    """Convert an official-store {major,minor,patch} dict to our tuple form."""
    try:
        return (int(d["major"]), int(d["minor"]), int(d["patch"]))
    except (KeyError, TypeError, ValueError):
        return None


def _sanitize_scan(scan, addon):
    """Return a well-formed VirusTotal scan dict, or None.

    The official feed occasionally emits scanResults: null, which NVDA logs as
    "Malformed add-on scan results". Keep only fully-formed dicts.
    """
    if not isinstance(scan, dict):
        return None
    vt = scan.get("virusTotal")
    stats = None
    try:
        stats = vt[0].get("last_analysis_stats")
    except (IndexError, AttributeError, TypeError):
        return None
    if not isinstance(stats, dict):
        return None
    return {
        "virusTotal": [
            {"last_analysis_stats": stats}
        ],
        "vtScanUrl": (addon.get("vtScanUrl") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Pinned variants: add-ons taken directly from a GitHub repo's releases and
# published under a distinct add-on ID so they can be chosen alongside the
# original. See pinned.json.
# ---------------------------------------------------------------------------

def _load_pinned_config(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("pinned", [])
    except FileNotFoundError:
        return []


def _load_excluded(path):
    """Return the list of community add-on names superseded by pinned variants.

    A pinned variant renames the bundle's manifest `name` to a distinct add-on
    ID, but the upstream catalogs still list the same add-on under its original
    generic `name` (e.g. the four "Eloquence" forks all publish `name =
    Eloquence`). Without this exclusion both the generic and the pinned entries
    would appear, duplicating the add-on. See pinned.json "exclude".
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("exclude", [])
    except FileNotFoundError:
        return []


def fetch_pinned(config_path=PINNED_CONFIG_PATH):
    """Fetch pinned variant add-ons from GitHub releases.

    The bundle's manifest `name` is rewritten to `addon_id` so the variant is
    listed as its own add-on (avoiding an ID collision with the original).
    The repackaged bundle is what gets hashed and linked, so NVDA's checksum
    verification applies to exactly the file we serve.
    """
    return _fetch_pinned_impl(config_path)


def _fetch_pinned_impl(config_path):
    pinned = _load_pinned_config(config_path)
    entries = []
    failures = []
    for spec in pinned:
        repo = spec.get("repo")
        url = spec.get("url")
        addon_id = spec.get("addon_id")
        if not addon_id or not (repo or url):
            message = f"pinned entry missing addon_id and repo/url: {spec!r}"
            log(message)
            failures.append(message)
            continue
        label = repo or url
        try:
            if repo:
                entries.extend(_fetch_one_pinned(spec, repo, addon_id))
            else:
                entries.extend(_fetch_one_pinned_url(spec, url, addon_id))
        except Exception as exc:  # noqa: BLE001 - report every failed pin together
            message = f"pinned entry {label} failed: {exc}"
            log(message)
            failures.append(message)
    if failures:
        raise RuntimeError(
            "refusing to publish an incomplete pinned add-on set: "
            + "; ".join(failures)
        )
    return entries


def cached_pinned_bundle(asset):
    # These bundles must be repackaged for publication, so keep their bytes
    # across runners too. Asset identity/update time invalidates replacements.
    bundle_key = hashlib.sha256(json.dumps([
        asset.get("id"), asset.get("updated_at"), asset.get("size"),
        asset["browser_download_url"],
    ]).encode("utf-8")).hexdigest()
    bundle_path = os.path.join(PINNED_BUNDLE_CACHE_PATH, bundle_key)
    if os.path.isfile(bundle_path):
        with open(bundle_path, "rb") as bundle_file:
            raw = bundle_file.read()
    else:
        log(f"Pinned package request: {asset['browser_download_url']}")
        raw = http_get(asset["browser_download_url"], headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/octet-stream",
        })
        # Validate before persisting, so a broken response is never cached.
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            bundle.read("manifest.ini")
        os.makedirs(PINNED_BUNDLE_CACHE_PATH, exist_ok=True)
        with open(bundle_path + ".tmp", "wb") as bundle_file:
            bundle_file.write(raw)
        os.replace(bundle_path + ".tmp", bundle_path)
    return raw


def _fetch_one_pinned(spec, repo, addon_id):
    api_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    if GITHUB_TOKEN:
        api_headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    releases = json.loads(http_get(
        f"{GITHUB_API}/repos/{repo}/releases?per_page=20", headers=api_headers
    ).decode("utf-8"))
    glob = spec.get("asset_glob", "*.nvda-addon")

    release = next((r for r in releases if not r.get("prerelease")), None)
    if release is None:
        raise RuntimeError("no non-prerelease release found")
    assets = [a for a in release.get("assets", []) if fnmatch.fnmatch(a["name"], glob)]
    if not assets:
        raise RuntimeError(f"no asset matching {glob!r} in release {release['tag_name']}")
    asset = assets[0]

    # The asset download hits github.com (redirected to the release CDN), not
    # api.github.com, so it does not need (or want) the Authorization header.
    raw = cached_pinned_bundle(asset)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        manifest_text = zf.read("manifest.ini").decode("utf-8")

    original_name = _manifest_name(manifest_text)
    if original_name == addon_id:
        log(f"pinned {repo}: manifest already named {addon_id}")
    summary = spec.get("summary") or _manifest_value(manifest_text, "summary") or release["name"] or addon_id
    description = _manifest_value(manifest_text, "description") or ""
    author = spec.get("publisher") or _manifest_value(manifest_text, "author") or ""
    mv = _manifest_value(manifest_text, "version")
    version = _select_pinned_version(mv, asset["name"], release["tag_name"])
    if not _pinned_fork_release_qualifies(spec, repo, sanitize_version(version)):
        raise RuntimeError(
            f"pinned fork {repo} release {version!r} is not newer than its "
            "parent; use fork_policy 'include' only for an intentionally "
            "distinct, separately named variant"
        )
    min_nvda = (parse_api_version(_manifest_value(manifest_text, "minimumNVDAVersion"))
                or parse_api_version(spec.get("min_nvda_version") or ""))
    last_tested = (parse_api_version(_manifest_value(manifest_text, "lastTestedNVDAVersion"))
                   or parse_api_version(spec.get("last_tested_nvda_version") or ""))

    # Rewrite manifest name -> addon_id, keep everything else.
    new_manifest = _rename_manifest_name(manifest_text, addon_id)
    patched = _repack_addon(raw, new_manifest.encode("utf-8"))

    digest = hashlib.sha256(patched).hexdigest()

    entry = {
        "name": addon_id,
        "summary": summary,
        "description": description,
        "author": author,
        "version": version,
        "channel": spec.get("channel", "stable"),
        "homepage": f"https://github.com/{repo}",
        "source_url": f"https://github.com/{repo}",
        "license": spec.get("license", "Unknown"),
        "license_url": spec.get("license_url", ""),
        "changelog": (release.get("body") or "").strip(),
        "download_url": f"https://github.com/{repo}/releases/download/{release['tag_name']}/{asset['name']}",
        "submission_ms": parse_iso8601_to_ms(release.get("published_at")),
        "min_nvda": min_nvda,
        "last_tested": last_tested,
        "source": "pinned",
        # Pre-computed: the build hashed the repackaged bundle above.
        "sha256": digest,
        "_patched_bytes": patched,
    }
    return [entry]


def _pinned_url_asset(url):
    """Describe a website-hosted bundle without downloading it.

    Add-ons distributed from an author's own site have no release metadata, so
    the HTTP validators stand in for it: ETag, Last-Modified and length give
    ``cached_pinned_bundle`` an identity that changes exactly when the author
    replaces the file. A host that answers no HEAD leaves the URL as the only
    identity, which is noted rather than silently accepted -- a replacement
    there is picked up when the cache is cleared, not before.
    """
    request = Request(quote_url(url), headers={
        "User-Agent": USER_AGENT, "Accept": "*/*",
    }, method="HEAD")
    etag = last_modified = length = None
    try:
        with urlopen(request, timeout=60) as response:
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
            length = response.headers.get("Content-Length")
    except (HTTPError, URLError, OSError) as exc:
        log(f"pinned url {url}: HEAD unavailable ({exc}); "
            "a replaced file will not be noticed until the cache is cleared")
        # HTTPError is also a response object. Close its body here so a host
        # that rejects HEAD does not leave its temporary response file for the
        # garbage collector to warn about after the build.
        if isinstance(exc, HTTPError):
            exc.close()
    if not (etag or last_modified or length):
        log(f"pinned url {url}: host supplies no validators")
    file_name = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
    return {
        "id": etag,
        "updated_at": last_modified,
        "size": length,
        "browser_download_url": url,
        "name": file_name,
    }, last_modified


def _fetch_one_pinned_url(spec, url, addon_id):
    """Publish an add-on its author distributes from their own website.

    No catalog lists these and they are not on GitHub, so nothing else in the
    build can see them. The bundle is validated and its manifest read exactly
    as a GitHub pin's is. It is repackaged only when the pin renames the
    add-on: leaving an unrenamed bundle alone keeps the author's own download
    URL, so their download counter still sees real installs and the mirror
    does not become a second home for their file.
    """
    asset, last_modified = _pinned_url_asset(url)
    raw = cached_pinned_bundle(asset)
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        manifest_text = bundle.read("manifest.ini").decode("utf-8-sig")

    original_name = _manifest_name(manifest_text)
    if not original_name or original_name.casefold() in _TEMPLATE_NAMES:
        raise RuntimeError(f"{url} has no valid manifest name")

    manifest_version = _manifest_value(manifest_text, "version")
    version = _select_pinned_version(manifest_version, asset["name"], "")
    summary = (spec.get("summary") or _manifest_value(manifest_text, "summary")
               or addon_id)
    homepage = (spec.get("homepage") or _manifest_value(manifest_text, "url") or "")

    renamed = original_name != addon_id
    if renamed:
        patched = _repack_addon(
            raw, _rename_manifest_name(manifest_text, addon_id).encode("utf-8"),
        )
    else:
        patched = raw

    entry = {
        "name": addon_id,
        "summary": summary,
        "description": _manifest_value(manifest_text, "description") or "",
        "author": (spec.get("publisher")
                   or _manifest_value(manifest_text, "author") or ""),
        "version": version,
        "channel": spec.get("channel", "stable"),
        "homepage": homepage,
        "source_url": spec.get("source_url") or homepage or url,
        "license": spec.get("license", "Unknown"),
        "license_url": spec.get("license_url", ""),
        "changelog": (spec.get("changelog") or "").strip(),
        "download_url": url,
        "submission_ms": parse_http_date_to_ms(last_modified),
        "min_nvda": (parse_api_version(
                         _manifest_value(manifest_text, "minimumNVDAVersion"))
                     or parse_api_version(spec.get("min_nvda_version") or "")),
        "last_tested": (parse_api_version(
                            _manifest_value(manifest_text, "lastTestedNVDAVersion"))
                        or parse_api_version(spec.get("last_tested_nvda_version") or "")),
        "source": "pinned",
        "store_source_label": PINNED_URL_SOURCE_LABEL,
        "sha256": hashlib.sha256(patched).hexdigest(),
    }
    # Only a renamed bundle is ours to serve; an untouched one keeps pointing
    # at the author's URL, and hash_one must not try to host it.
    if renamed:
        entry["_patched_bytes"] = patched
    return [entry]


def _github_repository_fork_parent(repo):
    owner, name = repo.split("/", 1)
    metadata = _github_json(
        f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    )
    if not metadata.get("fork"):
        return None
    parent = metadata.get("parent") or {}
    parent_name = parent.get("full_name")
    if not parent_name:
        raise RuntimeError(f"GitHub fork has no originating repository: {repo}")
    return parent_name


def _github_fork_release_qualifies(repo, fork_version=None):
    """Return true for originals or forks released beyond their parent version."""
    parent = _github_repository_fork_parent(repo)
    if not parent:
        return True
    if fork_version is None:
        _candidates, fork_state = _github_release_asset_state(repo)
        fork_version = _release_state_version(fork_state)
    _parent_candidates, parent_state = _github_release_asset_state(parent)
    parent_version = _release_state_version(parent_state)
    qualifies = (
        fork_version is not None
        and parent_version is not None
        and fork_version > parent_version
    )
    if not qualifies:
        log(
            f"GitHub fork {repo}: release {fork_version or 'unknown'} is not newer "
            f"than parent {parent} release {parent_version or 'unknown'}"
        )
    return qualifies


def _pinned_fork_release_qualifies(spec, repo, fork_version):
    """Apply the configured fork policy for an explicitly pinned variant.

    Pinned variants normally follow the global rule that a fork must release a
    newer numeric version than its parent. ``fork_policy: include`` is a
    deliberate exception for a separately named variant whose value is in its
    implementation differences rather than a newer upstream version.
    """
    policy = spec.get("fork_policy", "newer")
    if policy == "include":
        return True
    if policy != "newer":
        raise RuntimeError(
            f"invalid pinned fork_policy {policy!r} for {repo}; "
            "expected 'newer' or 'include'"
        )
    return _github_fork_release_qualifies(repo, fork_version)


def _manifest_name(manifest_text):
    for line in manifest_text.splitlines():
        m = re.match(r"^name\s*=\s*(.+)$", line.strip())
        if m:
            return m.group(1).strip().strip('"')
    return ""


def _manifest_value(manifest_text, key):
    lines = manifest_text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{key}\s*=\s*(.*)$", line.strip())
        if m:
            val = m.group(1).strip().strip('"')
            # triple-quoted values
            if val.startswith('"""') and not val.endswith('"""'):
                rest = []
                for cont in lines[i + 1:]:
                    if cont.strip().endswith('"""'):
                        rest.append(cont.strip()[:-3])
                        break
                    rest.append(cont.strip())
                return val[3:] + "\n" + "\n".join(rest)
            return val
    return ""


def _rename_manifest_name(manifest_text, new_name):
    return re.sub(r"^name\s*=.*$", f"name = {new_name}", manifest_text,
                  count=1, flags=re.MULTILINE)


def _repack_addon(raw, new_manifest):
    """Rebuild the .nvda-addon zip with a replaced manifest.ini.

    Passes the original ZipInfo through to writestr (rather than the bare
    filename) so the repack preserves each entry's date_time. Passing a bare
    name would stamp every entry with "now", so the repacked bundle's bytes --
    and therefore its sha256 and the whole mirror's cacheHash -- would change
    on every build even when nothing upstream changed, churning the published
    catalog for every NVDA client.
    """
    src = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dest:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "manifest.ini":
                data = new_manifest
            dest.writestr(info, data)
    src.close()
    return out.getvalue()


def _version_from_filename(download_name):
    """Best-effort version from a .nvda-addon asset filename, or None.

    bestmidi sometimes reports version "Unknown" while the asset filename
    carries the real version (e.g. "TeleNVDA.Accessolutions-2026.08.26.1049",
    "mygrammarplugin-2024.03.24", "Eloquence-19.1.3-RS"). Extracts the
    trailing dotted-numeric run so the entry can still be published.
    """
    if not download_name:
        return None
    stem = re.sub(r"\.nvda-addon$", "", download_name, flags=re.IGNORECASE).strip()
    if not stem:
        return None
    match = re.search(
        r"(?<![0-9A-Za-z])[vV]?(\d+(?:[._]\d+){1,3})(?:[-_.](?:dev|beta|rc|rs)\d*)?$",
        stem,
    )
    if not match:
        return None
    return match.group(1).replace("_", ".")


def version_is_uninformative(version):
    """True when a version tells NVDA nothing about which release this is.

    Covers both a free-form string ("unknown", "current") and a catalog that
    states a degenerate number: nvda-addons.ru publishes CodeFactoryOnlineTTS
    as version "0" while its own filename says 1.1. Either way the comparison
    value is 0.0.0, so NVDA can never see a newer release, and the real
    version has to come from the download itself.
    """
    return (sanitize_version(version) or (0, 0, 0)) == (0, 0, 0)


def better_version(current, candidate):
    """Return candidate when it says more about the release than current.

    Recovery must never talk an add-on backwards: a candidate is taken only
    when the current version is unusable, or when the candidate is genuinely
    the higher release.
    """
    parsed = sanitize_version(candidate)
    if parsed is None or parsed == (0, 0, 0):
        return None
    existing = sanitize_version(current)
    if existing is None or parsed > existing:
        return candidate
    return None


def recover_uninformative_versions(entries):
    """Fill in versions the catalogs failed to state, from the file name.

    Free and offline: the download URL is already in hand. Whatever this
    cannot answer is left for the manifest read in the hashing pass, which
    costs no extra request but does need the bytes.
    """
    recovered = 0
    for entry in entries:
        if not version_is_uninformative(entry.get("version")):
            continue
        file_name = unquote(urlsplit(entry.get("download_url") or "").path)
        candidate = _version_from_filename(file_name.rsplit("/", 1)[-1])
        replacement = better_version(entry.get("version"), candidate)
        if replacement:
            log(f"{entry.get('name')}: version {entry.get('version')!r} -> "
                f"{replacement!r} from the file name")
            entry["version"] = replacement
            recovered += 1
    return recovered


def _select_pinned_version(manifest_version, asset_name, release_tag):
    """Return the newest usable version advertised by a pinned release.

    Release authors sometimes upload a correctly named new asset while leaving
    an older ``version`` in manifest.ini. Trusting the manifest unconditionally
    makes NVDA see the new bytes as the old release and suppresses the update.
    Compare the manifest, asset filename, and release tag, preserving the
    original display spelling of whichever candidate has the highest numeric
    version.
    """
    candidates = []
    for value in (
        (manifest_version or "").strip(),
        _version_from_filename(asset_name),
        _version_from_filename(release_tag),
    ):
        parsed = sanitize_version(value)
        if parsed is not None:
            candidates.append((parsed, value))
    if not candidates:
        # Nothing numeric anywhere. The add-on's own manifest is still a
        # better display string than the asset filename, which is often just
        # the add-on id.
        fallback = (manifest_version or "").strip()
        if not fallback:
            fallback = (asset_name or "").rsplit(".nvda-addon", 1)[0].strip()
        return fallback or (release_tag or "").strip()
    return max(candidates, key=lambda item: item[0])[1]


def _load_github_owners(path=GITHUB_OWNERS_PATH):
    """Load author accounts and their known add-on repositories."""
    try:
        with open(path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    owners = data.get("owners", [])
    if not isinstance(owners, list):
        owners = []
    by_login = {
        spec["login"].casefold(): spec
        for spec in owners
        if isinstance(spec, dict) and spec.get("login")
    }
    for login in data.get("logins", []):
        if isinstance(login, str) and login.strip():
            by_login.setdefault(login.casefold(), {"login": login.strip()})
    return list(by_login.values())


def _github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _github_json(url, timeout=120):
    """Fetch GitHub JSON with bounded secondary-rate-limit retries."""
    for attempt, delay in enumerate((0, 5, 15, 30)):
        if delay:
            time.sleep(delay)
        try:
            return http_get_json(url, timeout=timeout, headers=_github_headers())
        except HTTPError as exc:
            if exc.code not in (403, 429) and not 500 <= exc.code < 600:
                raise
            if attempt == 3:
                raise
    raise RuntimeError(f"GitHub request did not complete: {url}")


def _github_json_conditional(url, etag=None, timeout=120):
    """Fetch GitHub JSON and preserve ETags for quota-free 304 checks."""
    for attempt, delay in enumerate((0, 5, 15, 30)):
        if delay:
            time.sleep(delay)
        headers = _github_headers()
        if etag:
            headers["If-None-Match"] = etag
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8-sig"))
                return data, response.headers.get("ETag"), False
        except HTTPError as exc:
            if exc.code == 304:
                return None, etag, True
            if exc.code not in (403, 429) and not 500 <= exc.code < 600:
                raise
            if attempt == 3:
                raise
    raise RuntimeError(f"GitHub conditional request did not complete: {url}")


def _github_graphql(query, timeout=180):
    """Execute an authenticated GitHub GraphQL query with bounded retries."""
    if not GITHUB_TOKEN:
        raise RuntimeError("GitHub GraphQL discovery requires GITHUB_TOKEN")
    headers = _github_headers()
    headers["Content-Type"] = "application/json"
    payload = json.dumps({"query": query}).encode("utf-8")
    for attempt, delay in enumerate((0, 5, 15, 30)):
        if delay:
            time.sleep(delay)
        request = Request(
            f"{GITHUB_API}/graphql",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            if exc.code not in (403, 429) and not 500 <= exc.code < 600:
                raise
            if attempt == 3:
                raise
            continue
        if result.get("errors"):
            raise RuntimeError(f"GitHub GraphQL errors: {result['errors']!r}")
        return result.get("data") or {}
    raise RuntimeError("GitHub GraphQL request did not complete")


def _github_owner_repositories(spec):
    """Return configured owner repositories and their fork parents."""
    login = (spec.get("login") or "").strip()
    if not login:
        raise ValueError("GitHub owner entry has no login")
    excluded = {
        name.strip().casefold()
        for name in spec.get("exclude_repositories", [])
        if isinstance(name, str) and name.strip()
    }
    exclude_forks = spec.get("fork_policy") == "exclude"
    known = {
        f"{login}/{name.strip()}"
        for name in spec.get("repositories", [])
        if (
            isinstance(name, str)
            and name.strip()
            and name.strip().casefold() not in excluded
        )
    }
    if not GITHUB_TOKEN:
        if not known:
            return [], {}
        log(f"GitHub owner {login}: no token; checking {len(known)} configured repositories")

    encoded_login = quote(login, safe="")
    try:
        repos = _github_json(
            f"{GITHUB_API}/users/{encoded_login}/repos?per_page=100&type=owner"
        )
    except HTTPError as exc:
        if exc.code != 404 or known:
            raise
        log(f"GitHub owner {login}: account no longer exists; skipping")
        return [], {}
    discovered = {
        repo["full_name"]
        for repo in repos
        if (
            isinstance(repo, dict)
            and repo.get("full_name")
            and repo["full_name"].split("/", 1)[-1].casefold() not in excluded
            and not (exclude_forks and repo.get("fork"))
        )
    }
    missing_known = known - discovered
    if missing_known:
        raise RuntimeError(
            f"GitHub owner {login} is missing configured repositories: "
            + ", ".join(sorted(missing_known))
        )
    log(f"GitHub owner {login}: discovered {len(discovered)} repositories")
    fork_parents = {}
    for repository in repos:
        if not isinstance(repository, dict) or not repository.get("fork"):
            continue
        full_name = repository.get("full_name")
        if not full_name or full_name not in discovered:
            continue
        parent_name = (repository.get("parent") or {}).get("full_name")
        if not parent_name:
            owner, name = full_name.split("/", 1)
            metadata = _github_json(
                f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            )
            parent_name = (metadata.get("parent") or {}).get("full_name")
        if not parent_name:
            raise RuntimeError(f"GitHub fork has no originating repository: {full_name}")
        fork_parents[full_name.casefold()] = parent_name
    return sorted(discovered, key=str.casefold), fork_parents


def _asset_family(filename):
    """Group versioned release files that represent the same packaged add-on."""
    stem = re.sub(r"\.nvda-addon$", "", filename or "", flags=re.IGNORECASE)
    family = re.sub(
        r"(?i)(?:[-_.](?:v(?:ersion)?)?)?\d+(?:[._-]\d+)*"
        r"(?:[-_.]?(?:alpha|beta|b|rc|dev)\d*)?$",
        "",
        stem,
    ).rstrip("-_. ")
    return (family or stem).casefold()


def _github_release_asset_state(repo, previous_state=None):
    """Return current candidates and conditional-request state for one repo."""
    owner, name = repo.split("/", 1)
    url = (
        f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        "/releases?per_page=20"
    )
    if not isinstance(previous_state, dict):
        previous_state = {}
    # Older caches predate release-version tracking. Force one full response so
    # fork-versus-parent comparisons are based on current release metadata.
    cached_etag = (
        previous_state.get("etag")
        if "latest_version" in previous_state
        else None
    )
    releases, etag, not_modified = _github_json_conditional(
        url,
        etag=cached_etag,
    )
    if not_modified:
        cached_candidates = previous_state.get("candidates")
        if not isinstance(cached_candidates, list):
            raise RuntimeError(f"GitHub returned 304 without cached candidates: {repo}")
        return cached_candidates, previous_state
    candidates = _release_asset_candidates_from_records(repo, releases or [])
    release_versions = [
        _release_tag_version(release.get("tag_name") or release.get("tagName") or "")
        for release in (releases or [])
        if not release.get("draft", release.get("isDraft", False))
    ]
    release_versions = [version for version in release_versions if version is not None]
    latest_version = max(release_versions) if release_versions else None
    return candidates, {
        "etag": etag,
        "candidates": candidates,
        "latest_version": list(latest_version) if latest_version else None,
    }


def _github_candidate_version(candidate):
    """Return the strongest numeric version advertised by a release candidate."""
    versions = [
        sanitize_version(_version_from_filename(candidate.get("asset_name"))),
        _release_tag_version(candidate.get("release_tag")),
    ]
    versions = [version for version in versions if version is not None]
    return max(versions) if versions else None


def _release_state_version(state):
    value = state.get("latest_version") if isinstance(state, dict) else None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        return tuple(int(part) for part in value)
    except (TypeError, ValueError):
        return None


def _release_asset_candidates_from_records(repo, releases):
    """Normalize REST or GraphQL release records into selected asset families."""
    selected = {}
    for release in releases:
        if release.get("draft", release.get("isDraft", False)):
            continue
        channel = (
            "beta"
            if release.get("prerelease", release.get("isPrerelease", False))
            else "stable"
        )
        raw_assets = release.get("assets", release.get("releaseAssets", []))
        if isinstance(raw_assets, dict):
            raw_assets = raw_assets.get("nodes", [])
        for asset in raw_assets:
            asset_name = asset.get("name") or ""
            if not asset_name.casefold().endswith(".nvda-addon"):
                continue
            key = (channel, _asset_family(asset_name))
            if key in selected:
                continue
            selected[key] = {
                "repo": repo,
                "channel": channel,
                "asset_name": asset_name,
                "download_url": (
                    asset.get("browser_download_url")
                    or asset.get("downloadUrl")
                    or ""
                ),
                "cache_key": "#".join(
                    str(value or "")
                    for value in (
                        asset.get("browser_download_url") or asset.get("downloadUrl"),
                        asset.get("updated_at") or asset.get("updatedAt"),
                        asset.get("size"),
                    )
                ),
                "release_tag": release.get("tag_name") or release.get("tagName") or "",
                "published_at": release.get("published_at") or release.get("publishedAt"),
                "changelog": release.get("body") or release.get("description") or "",
            }
    # Do not retain an obsolete prerelease when a newer stable asset from the
    # same family already exists. Keep incomparable build-style versions.
    for channel, family in list(selected):
        if channel != "beta" or ("stable", family) not in selected:
            continue
        beta = selected[("beta", family)]
        stable = selected[("stable", family)]
        beta_version = sanitize_version(_version_from_filename(beta["asset_name"]))
        stable_version = sanitize_version(_version_from_filename(stable["asset_name"]))
        if beta_version is not None and stable_version is not None and beta_version <= stable_version:
            del selected[("beta", family)]
    return list(selected.values())


def _github_owner_repository_names(owner_specs, batch_size=8):
    """Discover repository names and the parent of every fork."""
    specs_by_login = {
        spec["login"].casefold(): spec
        for spec in owner_specs
        if isinstance(spec, dict) and spec.get("login")
    }
    repositories = set()
    fork_parents = {}
    missing_logins = set()
    cursors = {login: None for login in specs_by_login}
    pending = list(specs_by_login)
    while pending:
        next_pending = []
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset:offset + batch_size]
            fields = []
            aliases = {}
            for index, login in enumerate(batch):
                alias = f"owner{index}"
                aliases[alias] = login
                after = (
                    f",after:{json.dumps(cursors[login])}"
                    if cursors[login]
                    else ""
                )
                requested_login = json.dumps(specs_by_login[login]["login"])
                fields.append(
                    f"{alias}:repositoryOwner(login:{requested_login}) {{"
                    " ... on User { repositories(first:100,ownerAffiliations:OWNER"
                    f"{after}) {{ nodes {{ nameWithOwner isFork"
                    " parent { nameWithOwner } }"
                    " pageInfo { hasNextPage endCursor } } }"
                    " ... on Organization { repositories(first:100"
                    f"{after}) {{ nodes {{ nameWithOwner isFork"
                    " parent { nameWithOwner } }"
                    " pageInfo { hasNextPage endCursor } } } }"
                )
            data = _github_graphql("query {" + "\n".join(fields) + "}")
            for alias, login in aliases.items():
                owner = data.get(alias)
                if owner is None:
                    # Accounts are deleted or renamed without warning. Drop the
                    # one account rather than halting every other author.
                    missing_logins.add(specs_by_login[login]["login"])
                    continue
                result = owner.get("repositories") or {}
                excluded = {
                    name.strip().casefold()
                    for name in specs_by_login[login].get("exclude_repositories", [])
                    if isinstance(name, str) and name.strip()
                }
                exclude_forks = (
                    specs_by_login[login].get("fork_policy") == "exclude"
                )
                for repo in result.get("nodes") or []:
                    repo_name = repo.get("nameWithOwner")
                    if (
                        not repo_name
                        or repo_name.split("/", 1)[-1].casefold() in excluded
                        or (exclude_forks and repo.get("isFork"))
                    ):
                        continue
                    repositories.add(repo_name)
                    parent_name = (repo.get("parent") or {}).get("nameWithOwner")
                    if repo.get("isFork") and parent_name:
                        fork_parents[repo_name.casefold()] = parent_name
                page_info = result.get("pageInfo") or {}
                if page_info.get("hasNextPage"):
                    cursors[login] = page_info.get("endCursor")
                    next_pending.append(login)
        pending = next_pending
    if missing_logins:
        if len(missing_logins) == len(specs_by_login):
            raise RuntimeError(
                "no configured GitHub account resolved, which points at an API "
                "problem rather than deleted accounts: "
                + ", ".join(sorted(missing_logins))
            )
        log(
            "GitHub owners no longer exist and were skipped: "
            + ", ".join(sorted(missing_logins))
        )
    return repositories, fork_parents


def _cached_github_repositories(cache):
    repositories = set()
    discovery = cache.get("__discovery__")
    if isinstance(discovery, dict):
        repositories.update(
            repo for repo in discovery.get("addon_repositories", [])
            if isinstance(repo, str) and "/" in repo
        )
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        source_url = entry.get("source_url") or ""
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/?", source_url)
        if match:
            repositories.add(match.group(1))
        repo = entry.get("_repo")
        if isinstance(repo, str) and "/" in repo:
            repositories.add(repo)
    return repositories


def _github_artifact_candidates(spec):
    """Select newest committed .nvda-addon files from configured artifact repos."""
    repo = (spec.get("repo") or "").strip()
    ref = (spec.get("ref") or "main").strip()
    if not repo or "/" not in repo:
        raise ValueError(f"invalid GitHub artifact repository: {spec!r}")
    if not _github_fork_release_qualifies(repo):
        return []
    owner, name = repo.split("/", 1)
    tree_url = (
        f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        f"/git/trees/{quote(ref, safe='')}?recursive=1"
    )
    tree = _github_json(tree_url)
    selected = {}
    for item in tree.get("tree", []):
        path = item.get("path") or ""
        if item.get("type") != "blob" or not path.casefold().endswith(".nvda-addon"):
            continue
        filename = path.rsplit("/", 1)[-1]
        parent = path.rsplit("/", 1)[0].casefold() if "/" in path else ""
        version_name = _version_from_filename(filename)
        version = sanitize_version(version_name) or (0, 0, 0)
        key = (parent, _asset_family(filename))
        previous = selected.get(key)
        if previous is not None and previous[0] >= version:
            continue
        raw_path = quote(path, safe="/")
        selected[key] = (
            version,
            {
                "repo": repo,
                "channel": "stable",
                "asset_name": filename,
                "download_url": (
                    f"https://raw.githubusercontent.com/{quote(owner, safe='')}/"
                    f"{quote(name, safe='')}/{quote(ref, safe='')}/{raw_path}"
                ),
                "cache_key": (
                    f"https://raw.githubusercontent.com/{quote(owner, safe='')}/"
                    f"{quote(name, safe='')}/{quote(ref, safe='')}/{raw_path}"
                    f"#{item.get('sha') or ''}"
                ),
                "release_tag": "",
                "published_at": None,
                "changelog": "",
            },
        )
    return [candidate for _version, candidate in selected.values()]


def _owner_cache_key(item):
    """Cache key for one owner asset entry, carrying the schema version.

    The version is applied here rather than where the key is built, because a
    candidate's key is persisted inside the per-repo state cache and replayed
    verbatim whenever GitHub answers 304. A version baked in at construction
    would therefore survive a bump and go on serving entries derived by older
    code -- which is exactly how add-ons stayed published under a description
    their manifest had never named them.
    """
    return (
        f"{GITHUB_OWNER_CACHE_VERSION}#"
        f"{item.get('cache_key') or item.get('download_url') or ''}"
    )


def _github_asset_entry(candidate):
    """Download one author-owned bundle, validate its manifest, and normalize it."""
    raw = http_get(candidate["download_url"], timeout=120)
    digest = hashlib.sha256(raw).hexdigest()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest_text = archive.read("manifest.ini").decode("utf-8-sig")

    name = _manifest_name(manifest_text)
    if not name or name.casefold() in _TEMPLATE_NAMES:
        raise RuntimeError(
            f"{candidate['repo']} asset {candidate['asset_name']} has no valid manifest name"
        )
    manifest_version = _manifest_value(manifest_text, "version")
    version = _select_pinned_version(
        manifest_version,
        candidate["asset_name"],
        # Release tags can be dates or unrelated build IDs. Author bundles use
        # the manifest and filename; this still catches stale manifests without
        # turning a tag such as kiraly-2026.08.23 into the add-on version.
        "",
    )
    manifest_channel = _norm_channel_bestmidi(
        _manifest_value(manifest_text, "updateChannel")
    )
    channel = candidate["channel"]
    if channel == "stable" or manifest_channel in ("beta", "dev"):
        channel = manifest_channel

    repo_url = f"https://github.com/{candidate['repo']}"
    return {
        "name": name,
        "summary": _manifest_value(manifest_text, "summary") or name,
        "description": _manifest_value(manifest_text, "description") or "",
        "author": _manifest_value(manifest_text, "author") or candidate["repo"].split("/", 1)[0],
        "version": version,
        "channel": channel,
        "homepage": _manifest_value(manifest_text, "url") or repo_url,
        "source_url": repo_url,
        "license": "Unknown",
        "license_url": "",
        "changelog": candidate.get("changelog") or "",
        "download_url": candidate["download_url"],
        "submission_ms": parse_iso8601_to_ms(candidate.get("published_at")),
        "min_nvda": parse_api_version(
            _manifest_value(manifest_text, "minimumNVDAVersion")
        ),
        "last_tested": parse_api_version(
            _manifest_value(manifest_text, "lastTestedNVDAVersion")
        ),
        "source": "github_owner",
        "sha256": digest,
    }


def _load_github_owner_cache(path=GITHUB_OWNER_CACHE_PATH):
    try:
        with open(path, "r", encoding="utf-8") as cache_file:
            data = json.load(cache_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_github_owner_cache(cache, path=GITHUB_OWNER_CACHE_PATH):
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as cache_file:
        json.dump(cache, cache_file, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, path)


def _filter_fork_candidates(
    candidates,
    fork_parents,
    release_state,
    parent_release_state,
):
    """Keep fork assets only when their version is newer than the parent release."""
    fork_repositories = {
        candidate.get("repo", "").casefold()
        for candidate in candidates
        if candidate.get("repo", "").casefold() in fork_parents
    }
    parents = {
        fork_parents[repo].casefold(): fork_parents[repo]
        for repo in fork_repositories
    }
    states = dict(parent_release_state)
    missing_parents = {
        key: name
        for key, name in parents.items()
        if key not in release_state
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(
                _github_release_asset_state,
                parent,
                parent_release_state.get(parent_key),
            ): (parent_key, parent)
            for parent_key, parent in missing_parents.items()
        }
        for future in concurrent.futures.as_completed(futures):
            parent_key, parent = futures[future]
            _parent_candidates, state = future.result()
            states[parent_key] = state

    kept = []
    rejected = []
    for candidate in candidates:
        repo_key = candidate.get("repo", "").casefold()
        parent = fork_parents.get(repo_key)
        if not parent:
            kept.append(candidate)
            continue
        fork_version = _github_candidate_version(candidate)
        parent_key = parent.casefold()
        parent_state = release_state.get(parent_key) or states.get(parent_key)
        parent_version = _release_state_version(parent_state)
        if (
            fork_version is not None
            and parent_version is not None
            and fork_version > parent_version
        ):
            kept.append(candidate)
            continue
        reason = (
            f"fork release {fork_version or 'unknown'} is not newer than "
            f"parent {parent} release {parent_version or 'unknown'}"
        )
        rejected.append({
            "addonId": candidate.get("asset_name"),
            "source": "github_owner",
            "reason": reason,
        })
        log(f"GitHub fork {candidate.get('repo')}: {reason}")
    return kept, states, rejected


def fetch_github_owners(
    config_path=GITHUB_OWNERS_PATH,
    cache_path=GITHUB_OWNER_CACHE_PATH,
    existing_entries=None,
):
    """Fetch every released add-on owned by the configured GitHub authors.

    A failed repository or bundle aborts the source rather than silently
    publishing an incomplete author set. Authenticated builds discover all
    current owner repositories; the configured list is the unauthenticated
    baseline and also guards against renamed or unexpectedly missing repos.
    A rate-limited repository reuses its last verified release state until
    the next run re-checks it for free through its stored ETag.
    """
    global GITHUB_OWNER_REJECTIONS
    GITHUB_OWNER_REJECTIONS = []
    owners = _load_github_owners(config_path)
    old_cache = _load_github_owner_cache(cache_path)
    discovery = old_cache.get("__discovery__")
    if not isinstance(discovery, dict):
        discovery = {}
    addon_repositories = _cached_github_repositories(old_cache)
    scanned_repositories = {
        repo for repo in discovery.get("scanned_repositories", [])
        if isinstance(repo, str) and "/" in repo
    }
    scanned_folded = {repo.casefold() for repo in scanned_repositories}
    pending_repositories = {
        repo for repo in discovery.get("pending_repositories", [])
        if isinstance(repo, str) and "/" in repo
    }
    if scanned_repositories:
        addon_repositories = {
            repo for repo in addon_repositories
            if repo.casefold() in scanned_folded
        }
    release_state = discovery.get("release_state")
    if not isinstance(release_state, dict):
        release_state = {}
    fork_parents = discovery.get("fork_parents")
    if not isinstance(fork_parents, dict):
        fork_parents = {}
    fork_parents = {
        repo.casefold(): parent
        for repo, parent in fork_parents.items()
        if isinstance(repo, str) and isinstance(parent, str) and "/" in parent
    }
    parent_release_state = discovery.get("parent_release_state")
    if not isinstance(parent_release_state, dict):
        parent_release_state = {}
    new_release_state = {
        repo: state
        for repo, state in release_state.items()
        if not scanned_repositories
        or repo.casefold() in scanned_folded
    }
    last_owner_scan = discovery.get("last_owner_scan") or 0
    configured_owners = sorted(
        [
            {
                "login": spec["login"].casefold(),
                "exclude_repositories": sorted(
                    name.strip().casefold()
                    for name in spec.get("exclude_repositories", [])
                    if isinstance(name, str) and name.strip()
                ),
                "fork_policy": spec.get("fork_policy", "newer-release-only"),
            }
            for spec in owners
            if isinstance(spec, dict) and spec.get("login")
        ],
        key=lambda spec: spec["login"],
    )
    repositories = set(addon_repositories)
    artifact_specs = []
    for spec in owners:
        artifact_specs.extend(spec.get("artifact_repositories", []))

    if GITHUB_TOKEN:
        owner_scan_due = (
            time.time() - last_owner_scan >= GITHUB_OWNER_DISCOVERY_TTL_SECONDS
            or discovery.get("configured_owners") != configured_owners
            or discovery.get("fork_policy") != "newer-release-only-v1"
        )
        if owner_scan_due:
            try:
                discovered_repositories, discovered_fork_parents = (
                    _github_owner_repository_names(owners)
                )
            except RuntimeError as exc:
                if "RATE_LIMITED" not in str(exc) or not addon_repositories:
                    raise
                log(
                    "GitHub owner repository discovery was rate-limited; "
                    "checking all cached add-on repositories and retrying discovery later"
                )
            else:
                discovered_folded = {
                    repo.casefold() for repo in discovered_repositories
                }
                addon_repositories = {
                    repo for repo in addon_repositories
                    if repo.casefold() in discovered_folded
                }
                new_release_state = {
                    repo: state for repo, state in new_release_state.items()
                    if repo.casefold() in discovered_folded
                }
                repositories = set(addon_repositories)
                pending_repositories.update(
                    discovered_repositories - scanned_repositories
                )
                pending_repositories = {
                    repo for repo in pending_repositories
                    if repo.casefold() in discovered_folded
                }
                scanned_repositories = discovered_repositories
                fork_parents = discovered_fork_parents
                last_owner_scan = int(time.time())
                log(
                    f"GitHub owners: discovered {len(discovered_repositories)} total "
                    "repositories; new repositories were added to this update check"
                )
        candidates = []
    else:
        for spec in owners:
            owner_repositories, owner_fork_parents = _github_owner_repositories(spec)
            repositories.update(owner_repositories)
            fork_parents.update(owner_fork_parents)
        candidates = []
    if pending_repositories:
        batch = sorted(pending_repositories, key=str.casefold)
        batch = batch[:GITHUB_NEW_REPOSITORY_BUDGET]
        repositories.update(batch)
        pending_repositories.difference_update(batch)
        log(
            f"GitHub owners: checking {len(batch)} repositories for the first "
            f"time; {len(pending_repositories)} queued for later runs"
        )
    configured_repositories = {
        f"{spec['login']}/{name.strip()}".casefold()
        for spec in owners
        if isinstance(spec, dict) and spec.get("login")
        for name in spec.get("repositories", [])
        if isinstance(name, str) and name.strip()
    }
    failures = []
    vanished = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(
                _github_release_asset_state,
                repo,
                release_state.get(repo.casefold()),
            ): ("release", repo)
            for repo in repositories
        }
        futures.update({
            pool.submit(_github_artifact_candidates, spec): ("artifact", spec.get("repo"))
            for spec in artifact_specs
        })
        for future in concurrent.futures.as_completed(futures):
            source_kind, source_name = futures[future]
            try:
                result = future.result()
                if source_kind == "release":
                    repo_candidates, repo_state = result
                    candidates.extend(repo_candidates)
                    new_release_state[source_name.casefold()] = repo_state
                else:
                    candidates.extend(result)
            except Exception as exc:  # noqa: BLE001 - aggregate source failures
                if (
                    source_kind == "release"
                    and isinstance(exc, HTTPError)
                    and exc.code in (403, 429)
                ):
                    stale = release_state.get(source_name.casefold())
                    if isinstance(stale, dict) and isinstance(
                        stale.get("candidates"), list
                    ):
                        # Rate limited: publishing the last verified release
                        # state keeps the catalog complete, and the next run
                        # re-checks the repository's ETag without new quota.
                        new_release_state[source_name.casefold()] = stale
                        candidates.extend(stale["candidates"])
                        log(
                            "GitHub rate limit hit; reusing last verified "
                            f"release state for {source_name}"
                        )
                        continue
                    if (
                        source_name.casefold() not in release_state
                        and source_name.casefold() not in configured_repositories
                    ):
                        # Never published, so nothing disappears by waiting.
                        pending_repositories.add(source_name)
                        continue
                if (
                    source_kind == "release"
                    and isinstance(exc, HTTPError)
                    and exc.code == 404
                    and source_name.casefold() not in configured_repositories
                ):
                    # A discovered repository was deleted, renamed or made
                    # private since the last scan. Forget it instead of
                    # blocking every other add-on until the next owner scan.
                    vanished.add(source_name)
                    continue
                failures.append(f"{source_name}: {exc}")
    if failures:
        raise RuntimeError(
            "refusing to publish incomplete GitHub author discovery: "
            + "; ".join(failures)
        )
    if vanished:
        log(
            "GitHub repositories no longer exist and were dropped: "
            + ", ".join(sorted(vanished, key=str.casefold))
        )
        vanished_folded = {repo.casefold() for repo in vanished}
        repositories = {
            repo for repo in repositories
            if repo.casefold() not in vanished_folded
        }
        addon_repositories = {
            repo for repo in addon_repositories
            if repo.casefold() not in vanished_folded
        }
        scanned_repositories = {
            repo for repo in scanned_repositories
            if repo.casefold() not in vanished_folded
        }
        new_release_state = {
            repo: state for repo, state in new_release_state.items()
            if repo.casefold() not in vanished_folded
        }
        fork_parents = {
            repo: parent for repo, parent in fork_parents.items()
            if repo not in vanished_folded
        }
        pending_repositories = {
            repo for repo in pending_repositories
            if repo.casefold() not in vanished_folded
        }

    addon_repositories.update(
        item["repo"] for item in candidates if item.get("repo")
    )
    candidates, parent_release_state, fork_rejections = _filter_fork_candidates(
        candidates,
        fork_parents,
        new_release_state,
        parent_release_state,
    )
    GITHUB_OWNER_REJECTIONS.extend(fork_rejections)
    discovery_snapshot = {
        "last_owner_scan": last_owner_scan,
        "configured_owners": configured_owners,
        "fork_policy": "newer-release-only-v1",
        "scanned_repositories": sorted(scanned_repositories, key=str.casefold),
        "pending_repositories": sorted(pending_repositories, key=str.casefold),
        "addon_repositories": sorted(addon_repositories, key=str.casefold),
        "release_state": new_release_state,
        "fork_parents": fork_parents,
        "parent_release_state": parent_release_state,
    }
    existing_by_url = {
        entry.get("download_url"): entry
        for entry in (existing_entries or [])
        if entry.get("download_url")
    }
    new_cache = {"__discovery__": discovery_snapshot}
    entries = []
    pending = []
    reused_catalog = 0
    reused_cache = 0
    for item in candidates:
        url = item.get("download_url")
        cache_key = _owner_cache_key(item)
        if not url:
            failures.append(f"{item.get('repo')}/{item.get('asset_name')}: no download URL")
            continue
        existing = existing_by_url.get(url)
        if existing is not None:
            entry = dict(existing)
            entry["source"] = "github_owner"
            entry["source_url"] = f"https://github.com/{item['repo']}"
            entries.append(entry)
            new_cache[cache_key] = entry
            reused_catalog += 1
            continue
        cached = old_cache.get(cache_key)
        if isinstance(cached, dict):
            if cached.get("_invalid"):
                GITHUB_OWNER_REJECTIONS.append({
                    "addonId": cached.get("asset_name") or item.get("asset_name"),
                    "source": "github_owner",
                    "reason": cached["_invalid"],
                })
                cached = dict(cached)
                cached["_repo"] = item.get("repo")
                new_cache[cache_key] = cached
                reused_cache += 1
                continue
            entry = dict(cached)
            entry["source"] = "github_owner"
            entry["source_url"] = f"https://github.com/{item['repo']}"
            entries.append(entry)
            new_cache[cache_key] = entry
            reused_cache += 1
            continue
        pending.append(item)

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_github_asset_entry, item): item for item in pending}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                entry = future.result()
                entries.append(entry)
                new_cache[_owner_cache_key(item)] = entry
            except HTTPError as exc:
                if exc.code not in (404, 410):
                    failures.append(
                        f"{item['repo']}/{item['asset_name']}: {exc}"
                    )
                    completed += 1
                    if completed % 25 == 0:
                        _write_github_owner_cache(new_cache, cache_path)
                    continue
                reason = f"unavailable GitHub release asset: HTTP {exc.code}"
                GITHUB_OWNER_REJECTIONS.append({
                    "addonId": item.get("asset_name"),
                    "source": "github_owner",
                    "reason": reason,
                })
                new_cache[_owner_cache_key(item)] = {
                    "_invalid": reason,
                    "asset_name": item.get("asset_name"),
                    "_repo": item.get("repo"),
                }
            except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, ValueError, RuntimeError) as exc:
                reason = f"invalid NVDA add-on bundle: {exc}"
                GITHUB_OWNER_REJECTIONS.append({
                    "addonId": item.get("asset_name"),
                    "source": "github_owner",
                    "reason": reason,
                })
                new_cache[_owner_cache_key(item)] = {
                    "_invalid": reason,
                    "asset_name": item.get("asset_name"),
                    "_repo": item.get("repo"),
                }
            except Exception as exc:  # noqa: BLE001 - aggregate bundle failures
                failures.append(
                    f"{item['repo']}/{item['asset_name']}: {exc}"
                )
            completed += 1
            if completed % 25 == 0:
                _write_github_owner_cache(new_cache, cache_path)
    new_cache["__discovery__"] = discovery_snapshot
    _write_github_owner_cache(new_cache, cache_path)
    if failures:
        raise RuntimeError(
            "refusing to publish incomplete GitHub author add-ons: "
            + "; ".join(failures)
        )
    log(
        f"GitHub authors: reused {reused_catalog} catalog records and "
        f"{reused_cache} cached manifests; validated {len(pending)} new assets"
    )
    return entries


def fetch_bestmidi():
    data = http_get_json(BESTMIDI_URL)
    entries = []
    for a in data.get("addons", []):
        name = (a.get("name") or "").strip()
        download_url = (a.get("download_url") or "").strip()
        version = (a.get("version") or "").strip()
        # bestmidi's version field can be missing OR stale even though the
        # release asset filename carries the current version. Compare both so
        # NVDA is not told that new bytes are an old release.
        version = _select_pinned_version(version, a.get("download_name"), "")
        entries.append(
            {
                "name": name,
                "summary": clean_text(a.get("summary")),
                "description": clean_text(a.get("description")),
                "author": (a.get("author") or "").strip() or (a.get("owner") or "").strip(),
                "version": version,
                "channel": _norm_channel_bestmidi(a.get("update_channel")),
                "homepage": (a.get("homepage_url") or "").strip(),
                "source_url": (a.get("source_url") or "").strip()
                or (a.get("repository_url") or "").strip(),
                "license": (a.get("addon_license") or "").strip(),
                "license_url": (a.get("addon_license_url") or "").strip(),
                "changelog": clean_text(a.get("changelog")),
                "download_url": download_url,
                "submission_ms": parse_iso8601_to_ms(a.get("last_pushed"))
                or parse_iso8601_to_ms(a.get("created_at")),
                "min_nvda": parse_api_version(a.get("minimum_nvda_version")),
                "last_tested": parse_api_version(a.get("last_tested_nvda_version")),
                "source": "bestmidi",
            }
        )
    return entries


def fetch_ru():
    data = http_get_json(RU_ADDONS_URL)
    entries = []
    for item in data:
        name = (item.get("name") or "").strip()
        links = item.get("links") or []
        if not links:
            continue
        link = links[0]
        url_field = (item.get("url") or "").strip()
        homepage = url_field if url_field.lower().startswith("http") else ""
        entries.append(
            {
                "name": name,
                "summary": clean_text(item.get("summary")),
                "description": clean_text(item.get("description")),
                "author": (item.get("author") or "").strip(),
                "version": (link.get("version") or "").strip(),
                "channel": _norm_channel_ru(
                    link.get("channel"), (link.get("version") or "").strip()
                ),
                "homepage": homepage,
                "source_url": homepage,
                "license": "",
                "license_url": "",
                "changelog": clean_text(link.get("changelog")),
                "download_url": (link.get("link") or "").strip(),
                "submission_ms": parse_ru_modified(link.get("modified")),
                "min_nvda": parse_api_version(link.get("minimum")),
                "last_tested": parse_api_version(link.get("lasttested")),
                "category": (item.get("category") or "").strip().lower(),
                "subcategory": (item.get("subcategory") or "").strip(),
                "source": "ru",
            }
        )
    return entries


def fetch_es():
    """Fetch the shared nvda.es / nvda-addons.org community catalog.

    Both domains currently return the same bytes. Use nvda.es as the primary
    and nvda-addons.org as failover so the mirror does not fetch and merge the
    same catalog twice. Every non-legacy download link becomes a candidate;
    ``keep_original_es_entries`` later removes add-ons already supplied by a
    stronger source.
    """
    data = None
    for url in ES_ADDONS_URLS:
        try:
            data = http_get_json(url)
            break
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            log(f"Spanish store source {url} failed: {exc}")
    if data is None:
        raise RuntimeError("could not fetch nvda.es or nvda-addons.org")

    entries = []
    for item in data:
        if item.get("hidden"):
            continue
        catalog_name = (item.get("name") or "").strip()
        for link in item.get("links") or []:
            raw_channel = (link.get("channel") or "stable").strip().lower()
            if raw_channel == "old":
                continue
            entries.append(
                {
                    "name": catalog_name,
                    "catalog_name": catalog_name,
                    "catalog_file": (link.get("file") or "").strip(),
                    "summary": clean_text(item.get("summary")),
                    "description": clean_text(item.get("description")),
                    "author": (item.get("author") or "").strip(),
                    "version": (link.get("version") or "").strip(),
                    "channel": _norm_channel_es(raw_channel),
                    "homepage": (item.get("url") or "").strip(),
                    "source_url": (item.get("url") or "").strip(),
                    "license": "",
                    "license_url": "",
                    "changelog": "",
                    "download_url": (link.get("link") or "").strip(),
                    "submission_ms": parse_es_modified(link.get("modified")),
                    "min_nvda": parse_api_version(link.get("minimum")),
                    "last_tested": parse_api_version(link.get("lasttested")),
                    "source": "es",
                }
            )
    return entries


def parse_es_modified(value):
    """Parse the Spanish catalog's naive modified timestamp as UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip()).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _normalized_addon_id(value):
    """Loose comparison key used only to match catalog aliases."""
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


_ES_ID_OVERRIDES = {
    # The feed uses the product name; manifest.ini uses codefactory-py3.
    "codefactory": "codefactory-py3",
}


def keep_original_es_entries(entries):
    """Keep Spanish-store candidates absent from every stronger source.

    The Spanish feed often uses display labels (spaces, punctuation, or a
    translated product name) where manifest.ini uses a compact internal ID.
    Match both its name and file slug against existing IDs before deciding an
    entry is original. This prevents aliases such as ``IF Interpreters`` and
    ``ifInterpreters`` from appearing as separate add-ons.
    """
    non_es = [entry for entry in entries if entry.get("source") != "es"]
    exact = {
        (entry["name"].casefold(), entry.get("channel") or "stable"): entry["name"]
        for entry in non_es
    }
    normalized = {
        (_normalized_addon_id(entry["name"]), entry.get("channel") or "stable"): entry["name"]
        for entry in non_es
        if _normalized_addon_id(entry["name"])
    }

    result = list(non_es)
    for entry in entries:
        if entry.get("source") != "es":
            continue
        candidates = (
            _ES_ID_OVERRIDES.get(entry.get("catalog_name", "").casefold()),
            entry.get("catalog_name"),
            entry.get("catalog_file"),
            entry.get("name"),
        )
        channel = entry.get("channel") or "stable"
        existing_name = None
        for candidate in candidates:
            if not candidate:
                continue
            existing_name = exact.get((candidate.casefold(), channel))
            if existing_name is None:
                existing_name = normalized.get((_normalized_addon_id(candidate), channel))
            if existing_name is not None:
                break
        if existing_name is None:
            result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Filter + transform
# ---------------------------------------------------------------------------

def reject_reason(entry):
    """Return a reason string if the entry should be rejected, else None."""
    name = (entry.get("name") or "").strip()
    download_url = (entry.get("download_url") or "").strip()

    if not name or name.lower() in _TEMPLATE_NAMES:
        return "missing or template add-on id"
    if entry.get("category") == "synth-voice":
        return "voice/data pack (skipped)"
    if entry.get("subcategory") in ("vosk", "silero", "vosk_tts"):
        return "voice/data model (skipped)"
    # A free-form version is never a reason to drop an add-on. Sources are
    # authoritative about what they list, and the add-on itself is the
    # authority on its own version: the hashing pass reads manifest.ini out
    # of the download it already makes and substitutes the real version
    # there. Only when the bundle states nothing usable either does the store
    # object fall back to 0.0.0 for NVDA's required numeric comparison field,
    # with the original text preserved for display.
    if not download_url:
        return "no download_url"
    return None


#: Static English translations for add-ons whose only available summary /
#: description is not English. Keyed by addonId. See translations.json.
TRANSLATIONS_PATH = "translations.json"
#: Generated by auto_translate.py and published with the site, then restored on
#: the next build. Merged UNDER translations.json, so a hand-written correction
#: is never overwritten by the model.
AUTO_TRANSLATIONS_PATH = "autoTranslations.json"
TRANSLATIONS = {}


def load_translations(path=TRANSLATIONS_PATH, auto_path=AUTO_TRANSLATIONS_PATH):
    """Load the English overlay: hand-maintained first, generated beneath.

    Merged per field rather than per add-on, so a human who corrected only a
    summary still gets the generated description, and never loses their summary
    to the model on the next run.
    """
    def read(candidate):
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if isinstance(data, dict):
            inner = data.get("translations", data)
            return inner if isinstance(inner, dict) else {}
        return {}

    merged = {}
    for addon_id, fields in read(auto_path).items():
        if isinstance(fields, dict):
            merged[addon_id] = dict(fields)
    for addon_id, fields in read(path).items():
        if isinstance(fields, dict):
            merged.setdefault(addon_id, {}).update(fields)
        else:
            merged[addon_id] = fields
    return merged


#: addonId -> the best English changelog any source published for it, filled in
#: by main() before transform() runs. See english_changelogs.
ENGLISH_CHANGELOGS = {}

CHANGELOG_UNAVAILABLE = "Release notes are not available in English."


def _is_non_english_changelog(changelog, addon_id):
    """True when release notes cannot be read by an English-speaking user."""
    return bool(
        _NON_LATIN_SCRIPT_RE.search(changelog)
        or _NON_ENGLISH_LATIN_CHANGELOG_RE.search(changelog)
        or addon_id in _KNOWN_NON_ENGLISH_CHANGELOG_IDS
    )


def english_changelogs(entries):
    """Map addon id -> the best English changelog any source published for it.

    Sources disagree about language, not only about versions: nvda-addons.ru
    republishes hundreds of add-ons the official store also carries, in its own
    "Dev" channel and with Russian release notes. dedupe keys on
    (addonId, channel), so that Russian entry never meets its English sibling
    and used to reach the store as "release notes not available in English"
    even though NV Access published English notes for the very same add-on.
    """
    best = {}
    for entry in entries:
        addon_id = entry.get("name") or ""
        changelog = clean_text(entry.get("changelog"))
        if not addon_id or not changelog:
            continue
        if _is_non_english_changelog(changelog, addon_id):
            continue
        rank = SOURCE_PRIORITY.get(entry.get("source"), 0)
        current = best.get(addon_id)
        if current is None or rank > current[0]:
            best[addon_id] = (rank, changelog)
    return {addon_id: text for addon_id, (_rank, text) in best.items()}


def _translate_entry(entry):
    """Overlay configured English metadata onto a catalog entry."""
    tr = TRANSLATIONS.get(entry["name"])
    if tr:
        for field in ("summary", "description", "author", "changelog"):
            if tr.get(field):
                entry[field] = tr[field]

    changelog = entry.get("changelog") or ""
    if _is_non_english_changelog(changelog, entry["name"]):
        # Borrow another source's English notes for the same add-on before
        # falling back to saying there are none.
        entry["changelog"] = (
            ENGLISH_CHANGELOGS.get(entry["name"]) or CHANGELOG_UNAVAILABLE
        )


def transform(entry, sha256):
    """Map a normalized entry to the NVDA add-on store object.

    Field conventions follow the official store data (addonStore.nvaccess.org):
    optional keys are OMITTED rather than set to null, because NVDA's
    VirusTotalScanResults.fromDict treats an explicit None as malformed scan
    data, and the store GUI renders some fields without a None guard
    (details.py _appendDetailsLabelValue -> AppendText raises on None).
    The reviews key is "reviewUrl" (lowercase "rl") -- NVDA reads
    addon.get("reviewUrl").
    """
    name = entry["name"]
    version = entry["version"]

    _translate_entry(entry)

    addon_version = sanitize_version(version)
    if addon_version is None:
        # Reached only when neither the catalog nor the bundle's manifest.ini
        # stated anything numeric. Publish the add-on rather than drop it:
        # addonVersionName still carries the real string, and only update
        # comparisons use the number.
        log(f"{name}: unparseable version {version!r}; publishing as 0.0.0")
        addon_version = (0, 0, 0)
    min_nvda = entry.get("min_nvda") or (0, 0, 0)
    last_tested = entry.get("last_tested") or (0, 0, 0)

    author = entry.get("author") or "Unknown"
    license_name = entry.get("license") or "Unknown"
    license_url = entry.get("license_url") or ""
    homepage = entry.get("homepage") or ""
    download_url = entry.get("download_url") or ""
    source_url = entry.get("source_url") or homepage or download_url

    obj = {
        "addonId": name,
        # Authors sometimes put a description in manifest.ini's summary,
        # which is the field NVDA shows as the add-on's name. Publishing that
        # verbatim leaves a paragraph where a title belongs, and the real name
        # nowhere at all -- unusable when arrowing a list of names.
        # An add-on whose own name is not English gets the English name first
        # and its real name after "AKA", so it is both understandable and
        # still recognisable from its own documentation.
        "displayName": compose_display_name(
            best_display_name(
                clean_text(entry.get("summary")),
                clean_text(entry.get("manifest_summary")),
                name,
            ) or name,
            clean_text(entry.get("manifest_summary")),
        ),
        "description": entry.get("description") or "",
        "publisher": author,
        "channel": entry.get("channel") or "stable",
        "addonVersionName": version,
        "addonVersionNumber": {
            "major": addon_version[0],
            "minor": addon_version[1],
            "patch": addon_version[2],
        },
        "license": license_name,
        "licenseURL": license_url,
        "sourceURL": source_url,
        "URL": download_url,
        # Official store entries bring their own upstream hash; community
        # entries get the one computed by this build.
        "sha256": entry.get("sha256") or sha256,
        "minNVDAVersion": {
            "major": min_nvda[0],
            "minor": min_nvda[1],
            "patch": min_nvda[2],
        },
        "lastTestedVersion": {
            "major": last_tested[0],
            "minor": last_tested[1],
            "patch": last_tested[2],
        },
        "submissionTime": entry.get("submission_ms") or 0,
        "legacy": False,
        # A pinned entry may come from a GitHub release or straight from an
        # author's website, so it carries its own label rather than being
        # described as a GitHub release either way.
        "storeSource": entry.get("store_source_label") or STORE_SOURCE_LABELS.get(
            entry.get("source"),
            entry.get("source") or "Unknown",
        ),
    }

    # Optional keys are present-or-absent, never null (see docstring).
    changelog = clean_text(entry.get("changelog"))
    if changelog:
        obj["changelog"] = changelog
    if homepage:
        # Keep absolute URLs only; a bare path renders as a broken link.
        if homepage.startswith("http"):
            obj["homepage"] = homepage
    scan = entry.get("scan_results")
    if scan:
        obj["scanResults"] = scan
        obj["vtScanUrl"] = scan["vtScanUrl"]

    return obj


def _has_cyrillic(text):
    """True when text contains Cyrillic (Russian) characters."""
    return bool(text and _CYRILLIC_RE.search(text))


def dedupe(entries):
    """Dedupe by (addonId, channel).

    The official store lists one entry PER CHANNEL for the same add-on (e.g.
    stable 2026.05.03 + dev 2024.09.09 of robEnhancements); NVDA's client
    indexes by [channel][addonId], so all variants must survive. Community
    catalogs list one entry per add-on, which lands in whatever channel the
    catalog declares.

    Preference order within the same (addonId, channel): explicitly pinned
    releases > direct author releases > official (NV Access-reviewed,
    VirusTotal data, upstream hash) > nvda-addons.ru (curated, direct links) >
    bestmidi > the shared Spanish catalog. Within one source the newer parseable
    version wins; an entry with a download URL beats one without.

    When the winning entry's text is Russian, English summary/description/
    changelog are adopted from a non-Cyrillic sibling (official first, then
    bestmidi), so the store shows English wherever an English source exists
    while keeping the winner's reliable download URL and hash.

    VirusTotal scan results describe a specific file, and the official store is
    the only source that scans bundles. When a non-official entry wins (e.g. a
    direct author release) but an official sibling served the identical file
    (same sha256), the winner keeps that sibling's scan results instead of
    dropping them; a differing hash means a different file, and its scan data
    is never carried over.
    """
    priority = SOURCE_PRIORITY
    by_key = {}
    for e in entries:
        key = (e["name"].casefold(), e.get("channel") or "stable")
        by_key.setdefault(key, []).append(e)

    result = []
    for group in by_key.values():
        winner = max(
            group,
            key=lambda e: (
                1 if e["download_url"] else 0,
                priority.get(e["source"], 0),
                sanitize_version(e.get("version")) or (0, 0, 0),
            ),
        )
        if _has_cyrillic(winner.get("summary")) or _has_cyrillic(winner.get("description")):
            english = sorted(
                group,
                key=lambda e: priority.get(e["source"], 0),
                reverse=True,
            )
            for cand in english:
                if _has_cyrillic(cand.get("summary")) or _has_cyrillic(cand.get("description")):
                    continue
                for field in ("summary", "description", "changelog"):
                    if cand.get(field):
                        winner[field] = cand[field]
                break
        if not winner.get("scan_results"):
            winner_sha = (winner.get("sha256") or "").strip().lower()
            if winner_sha:
                for cand in sorted(
                    group,
                    key=lambda e: priority.get(e["source"], 0),
                    reverse=True,
                ):
                    scan = cand.get("scan_results")
                    cand_sha = (cand.get("sha256") or "").strip().lower()
                    if scan and cand_sha == winner_sha:
                        winner["scan_results"] = scan
                        break
        result.append(winner)
    return result


def drop_redundant_channel_duplicates(entries):
    """Collapse a pre-release row that carries the same release as stable.

    dedupe() keys on (addonId, channel), which is right when the channels
    really do hold different releases. Often they do not: nvda-addons.ru labels
    most of its links "Dev" regardless of what upstream published, so the same
    version of the same add-on reached the store twice, once as stable and once
    as dev, and NVDA lists both. Versions are compared as the numbers NVDA
    itself compares, so "2026.05.03" and "2026.5.3" are recognised as one
    release despite the different spelling.

    Returns (kept, rejected).
    """
    stable_versions = {}
    for entry in entries:
        if (entry.get("channel") or "stable") != "stable":
            continue
        version = sanitize_version(entry.get("version"))
        if version is not None:
            stable_versions.setdefault(entry["name"].casefold(), set()).add(version)

    kept = []
    rejected = []
    for entry in entries:
        channel = entry.get("channel") or "stable"
        version = sanitize_version(entry.get("version"))
        if (
            channel != "stable"
            and version is not None
            and version in stable_versions.get(entry["name"].casefold(), ())
        ):
            rejected.append({
                "addonId": entry.get("name"),
                "source": entry.get("source"),
                "reason": f"same release as the stable channel (listed as {channel})",
            })
            continue
        kept.append(entry)
    return kept, rejected


def load_json_cache(path):
    """Load a persisted dict cache, or {} for anything unreadable.

    Used for the caches that ride along with the site (locale ETags, machine
    translations). A corrupt or missing file costs a rebuild of that cache,
    never a failed build.
    """
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def polled_source(name, cache, fetch, ttl=None, now=None):
    """Return one catalog's normalized entries, polling it at most every ``ttl``.

    These catalogs change less often than the hourly build, and none of them
    can be revalidated cheaply, so a build inside the poll interval reuses the
    entries the last poll produced instead of pulling the whole catalog again.
    That decouples how fast the mirror republishes from how hard it leans on
    four other people's servers.

    A failed poll is not fatal either: a host that is down or slow returns the
    last entries it did serve, so one bad minute upstream cannot delete a whole
    source from the catalog. Only a failure with nothing cached propagates,
    which is the existing behaviour for a first build.
    """
    ttl = SOURCE_POLL_SECONDS if ttl is None else ttl
    now = time.time() if now is None else now
    record = cache.get(name)
    if not isinstance(record, dict) or not isinstance(record.get("entries"), list):
        record = None
    if record and record.get("next_poll", 0) > now:
        age = int(now - record.get("polled_at", now))
        log(f"{name}: reusing the catalog polled {age // 60}m{age % 60:02d}s ago "
            f"({len(record['entries'])} entries; next poll in "
            f"{int(record['next_poll'] - now) // 60}m)")
        return copy.deepcopy(record["entries"])
    try:
        entries = fetch()
    except Exception as exc:  # noqa: BLE001 - any upstream failure, one policy
        if record is None:
            raise
        cache[name] = dict(record, next_poll=now + SOURCE_RETRY_SECONDS)
        log(f"{name}: poll failed ({exc}); reusing {len(record['entries'])} "
            "entries from the last good poll")
        return copy.deepcopy(record["entries"])
    cache[name] = {
        "polled_at": now,
        "next_poll": now + ttl,
        "entries": copy.deepcopy(entries),
    }
    return entries


def official_store_translations(locales, locale_cache, ttl=None, now=None):
    """Return {lang: translations} from the official store, swept on a schedule.

    A sweep is one request per language to a single host. An add-on gains an
    author-written translation about as often as it is released, so sweeping
    on every build would spend 73 requests apiece to learn nothing; between
    sweeps the strings the last one found are reused. The per-language ETags
    in ``locale_cache`` still make the sweep itself nearly free -- this only
    decides how often it happens.

    Mutates ``locale_cache`` in place, the way the caller already persists it.
    """
    ttl = LOCALE_POLL_SECONDS if ttl is None else ttl
    now = time.time() if now is None else now
    poll = locale_cache.get(LOCALE_POLL_KEY)
    languages = [lang for lang in locales if lang != "en"]
    if isinstance(poll, dict) and poll.get("next_poll", 0) > now:
        translations = {}
        for lang in languages:
            cached = locale_cache.get(lang)
            if isinstance(cached, dict) and isinstance(cached.get("translations"), dict):
                translations[lang] = cached["translations"]
        due_in = int(poll["next_poll"] - now)
        log(f"official store translations: reusing "
            f"{sum(len(t) for t in translations.values())} strings across "
            f"{len(translations)} languages; next sweep in "
            f"{due_in // 3600}h{due_in % 3600 // 60:02d}m")
        return translations
    baseline, locale_cache["en"] = fetch_official_english_baseline(
        locale_cache.get("en")
    )
    translations = {}
    for lang in languages:
        translations[lang], locale_cache[lang] = fetch_official_locale_translations(
            lang, baseline, locale_cache.get(lang),
        )
    locale_cache[LOCALE_POLL_KEY] = {"polled_at": now, "next_poll": now + ttl}
    log(f"official store translations: "
        f"{sum(len(t) for t in translations.values())} strings across "
        f"{len(translations)} languages")
    return translations


def load_hashcache(path):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def current_nvda_api_version():
    """Fetch NVDA's current add-on API version (year.major.minor), else None."""
    try:
        src = http_get(NVDA_BUILD_VERSION_URL, timeout=30).decode("utf-8")
    except (HTTPError, URLError, OSError):
        return None
    year = _master_build_version_re("version_year").search(src)
    major = _master_build_version_re("version_major").search(src)
    minor = _master_build_version_re("version_minor").search(src)
    if not (year and major and minor):
        log("current NVDA API version: buildVersion.py no longer matches the "
            "expected declarations; no dev API version will be published")
        return None
    return f"{year.group(1)}.{major.group(1)}.{minor.group(1)}"


def back_compat_to_version():
    """Fetch NVDA's BACK_COMPAT_TO tuple, else the hardcoded fallback.

    An add-on is compatible with NVDA when
    ``minimumNVDAVersion <= current`` and
    ``lastTestedNVDAVersion >= BACK_COMPAT_TO``. NVDA trusts the
    ``{apiVersion}.json`` endpoint to contain only such add-ons, so the mirror
    must apply the same rule when filtering that file (the "compatible" view).
    """
    try:
        src = http_get(NVDA_API_VERSION_URL, timeout=30).decode("utf-8")
    except (HTTPError, URLError, OSError) as exc:
        log(f"BACK_COMPAT_TO: could not read NVDA master ({exc}); "
            f"falling back to {FALLBACK_BACK_COMPAT_TO}")
        return FALLBACK_BACK_COMPAT_TO
    # The declaration carries a type annotation between the name and the "=":
    #     BACK_COMPAT_TO: AddonApiVersionT = (2027, 1, 0)
    # so the annotation has to be skipped explicitly. Matching ":" as if it
    # were the assignment operator silently never matches, which turns this
    # whole function into a constant.
    m = _MASTER_BACK_COMPAT_TO_RE.search(src)
    if not m:
        log(f"BACK_COMPAT_TO: NVDA master no longer matches the expected "
            f"declaration; falling back to {FALLBACK_BACK_COMPAT_TO}")
        return FALLBACK_BACK_COMPAT_TO
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def load_nvda_api_version_entries(path=NVDA_API_VERSIONS_PATH, refresh=False):
    """Load bundled API history, optionally merging in live datastore data.

    A scheduled build refreshes from nvaccess/addon-datastore so a new stable
    release is served without a mirror code change. Bundled history is retained
    if the live response is incomplete and is the offline fallback. Never raises.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            bundled = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        bundled = []
    if not isinstance(bundled, list):
        bundled = []
    if not refresh:
        return bundled
    try:
        live = http_get_json(NVDA_API_VERSIONS_URL, timeout=30)
    except (HTTPError, URLError, OSError, ValueError, TypeError):
        return bundled
    if not isinstance(live, list):
        return bundled
    return _merge_nvda_api_version_entries(bundled, live)


def _api_version_from_entry(entry):
    if not isinstance(entry, dict):
        return None
    api = entry.get("apiVer") or {}
    return parse_api_version(
        f"{api.get('major')}.{api.get('minor')}.{api.get('patch')}"
    )


def _merge_nvda_api_version_entries(bundled, live):
    """Merge cumulative history, with live metadata winning per API version."""
    merged = {}
    for entry in [*bundled, *live]:
        ver = _api_version_from_entry(entry)
        if ver is not None:
            merged[ver] = entry
    return list(merged.values())


def nvda_api_versions_from_entries(data):
    """Map "year.major.minor" to BACK_COMPAT_TO for API metadata entries."""
    result = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        back = entry.get("backCompatTo") or {}
        ver = _api_version_from_entry(entry)
        if ver is None:
            continue
        result[f"{ver[0]}.{ver[1]}.{ver[2]}"] = _ver_tuple(back)
    return result


def load_nvda_api_versions(path=NVDA_API_VERSIONS_PATH, refresh=False):
    """Load the per-release BACK_COMPAT_TO map. Never raises."""
    return nvda_api_versions_from_entries(
        load_nvda_api_version_entries(path=path, refresh=refresh)
    )


def published_nvda_api_versions(data, years_in_full=API_VERSION_YEARS_IN_FULL):
    """Select the Add-on Store-era API versions to publish, newest first.

    Every published version costs one full copy of the compatibility-filtered
    catalog per locale, so the set cannot grow without bound: NVDA adds roughly
    eight API versions a year and the site has a size ceiling.

    Retention keeps every release line from the newest ``years_in_full`` NVDA
    years, and only the newest line of each year before that. Superseded
    patches within a line are always dropped. See API_VERSION_YEARS_IN_FULL.

    ``years_in_full=0`` disables pruning beyond superseded patches, keeping
    every line's newest patch.
    """
    released = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ver = _api_version_from_entry(entry)
        if ver is not None and ver >= ADDON_STORE_FIRST_API_VERSION:
            released.add(ver)

    # One entry per (year, major) line: the newest patch in that line.
    newest_in_line = {}
    for ver in released:
        line = (ver[0], ver[1])
        if ver > newest_in_line.get(line, (0, 0, 0)):
            newest_in_line[line] = ver

    kept = set(newest_in_line.values())
    if years_in_full:
        # Years are ranked by what NVDA actually released, not by the calendar,
        # so a build running in a quiet stretch of a new year does not silently
        # retire the year everyone is still on.
        full_years = sorted({ver[0] for ver in kept}, reverse=True)[:years_in_full]
        newest_in_year = {}
        for ver in kept:
            if ver > newest_in_year.get(ver[0], (0, 0, 0)):
                newest_in_year[ver[0]] = ver
        kept = {
            ver for ver in kept
            if ver[0] in full_years or ver == newest_in_year[ver[0]]
        }
    return [
        f"{ver[0]}.{ver[1]}.{ver[2]}" for ver in sorted(kept, reverse=True)
    ]


def _log_api_version_retention(entries, api_versions):
    """Say plainly which releases this build stopped serving, and why.

    A pruned API version is not a degraded experience for anyone still on it:
    NVDA has no version fallback, so the request 404s and the Add-on Store is
    simply empty. That is worth one honest line in the build log rather than
    being discoverable only by diffing the deployed site.
    """
    published = set(api_versions)
    dropped = [
        ver for ver in published_nvda_api_versions(entries, years_in_full=0)
        if ver not in published
    ]
    if not dropped:
        return
    superseded = []
    lines_dropped = []
    kept_lines = {tuple(v.split(".")[:2]) for v in published if v != "latest"}
    for ver in dropped:
        (lines_dropped if tuple(ver.split(".")[:2]) not in kept_lines
         else superseded).append(ver)
    if superseded:
        log(f"API versions not published (superseded patch releases, NVDA "
            f"moves these users forward automatically): {', '.join(superseded)}")
    if lines_dropped:
        log(f"API versions not published (release line retired; anyone still "
            f"on one of these gets an EMPTY Add-on Store, raise "
            f"--api-version-years to keep them): {', '.join(lines_dropped)}")


def _report_site_size(out_dir, stats):
    """Record the built site's size and warn when it passes the Pages limit.

    Every published API version and every locale is a full copy of the
    catalog, so the site grows with each NVDA release whether or not anything
    else changed. Measuring the real output is the only honest number: a
    translated catalog is larger than an English one, and non-Latin locales
    cost two to three bytes per character where English costs one.
    """
    total = 0
    for root, _dirs, files in os.walk(out_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    stats["site_bytes"] = total
    log(f"Built site: {total/1e9:.2f} GB across {len(stats['api_versions'])} "
        f"API versions and {stats['locales']} locales")
    if total > SITE_SIZE_WARN_BYTES:
        log(f"WARNING: the built site is {total/1e9:.2f} GB, over the "
            f"{SITE_SIZE_WARN_BYTES/1e9:.2f} GB GitHub Pages soft limit. "
            f"Lower --api-version-years (currently publishing "
            f"{len(stats['api_versions'])} views) or move to a host without "
            f"this ceiling.")
    return total


def _ver_tuple(d):
    """{major, minor, patch} dict -> (major, minor, patch) int tuple."""
    try:
        return (int(d["major"]), int(d["minor"]), int(d["patch"]))
    except (KeyError, TypeError, ValueError):
        return (0, 0, 0)


def _compatible_for_api_version(output, api_version_tuple, back_compat_to):
    """Filter store objects to those compatible with the given API version.

    Mirrors NVDA's addonHandler.addonVersionCheck.isAddonCompatible:
    minimumNVDAVersion <= apiVersion AND lastTestedNVDAVersion >= BACK_COMPAT_TO.
    """
    compatible = []
    for obj in output:
        min_nvda = _ver_tuple(obj["minNVDAVersion"])
        last_tested = _ver_tuple(obj["lastTestedVersion"])
        if min_nvda <= api_version_tuple and last_tested >= back_compat_to:
            compatible.append(obj)
    return compatible


#: Openers that mark a description rather than a name. An add-on called
#: "The Clock" is fine; "An NVDA add-on that ..." is a sentence.
_PROSE_OPENER_RE = re.compile(
    r"^(an?|the|this|these|add-?ons?|nvda\s+add-?ons?|accessible\s+add-?ons?|"
    r"plugins?|complementos?|extensions?)\b", re.I)

#: Relative clauses and second-person address only occur in prose.
_PROSE_CLAUSE_RE = re.compile(
    r"\b(that|which|lets\s+you|let\s+you|allows?\s+you|enables?\s+you|"
    r"you\s+can|designed\s+to|featuring)\b", re.I)


def looks_like_prose_name(value):
    """True when a display name reads as a sentence rather than a name.

    Add-on authors sometimes put a description in manifest.ini's ``summary``,
    which is the field NVDA shows as the add-on's name. The store then lists a
    paragraph where every other row has a title, which is unusable to navigate
    by: a screen reader user arrowing the list hears a sentence instead of a
    name, and the real name appears nowhere at all.

    Deliberately conservative. Real names are often long ("Acapela TTS Voices
    for NVDA Engines", "BOA: Better Office Accessibility"), so length alone is
    never enough -- there has to be a grammatical signal too.
    """
    text = (value or "").strip()
    if not text:
        return False
    # A composed "English name, AKA original name" is a pair of names, not a
    # sentence. Judge the English half, so the combined length of two titles
    # is never mistaken for prose.
    text = text.split(AKA_SEPARATOR, 1)[0].strip() or text
    words = text.split()
    # A relative clause or second-person address is decisive at any length.
    if _PROSE_CLAUSE_RE.search(text):
        return True
    ends_sentence = text.endswith(".") and not text.endswith("...")
    opens_prose = bool(_PROSE_OPENER_RE.match(text)) and len(words) >= 4
    if ends_sentence and len(words) >= 5:
        return True
    if opens_prose and (ends_sentence or len(words) >= 6):
        return True
    # Long enough that no reasonable title is this shape.
    return len(words) >= 10


def humanized_addon_id(addon_id):
    """Turn an add-on id into a readable title as a last resort.

    "biosManager" -> "Bios Manager", "ricerca_testuale" -> "Ricerca Testuale".
    Only ever used when no source states a usable name: it is a worse name
    than the author's own, but it is a name, and it is what the add-on is
    called everywhere else in NVDA.
    """
    text = re.sub(r"[_\-.]+", " ", (addon_id or "").strip())
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    words = [w for w in text.split() if w]
    return " ".join(
        # "contrast-checker-nvda" should not become "Contrast Checker Nvda".
        _ADDON_ID_ACRONYMS.get(w.casefold())
        or (w if w[:1].isupper() else w.capitalize())
        for w in words
    )


#: Spellings to restore when an add-on id is turned back into a title.
_ADDON_ID_ACRONYMS = {
    "nvda": "NVDA", "ocr": "OCR", "tts": "TTS", "sapi": "SAPI", "url": "URL",
    "ui": "UI", "ai": "AI", "api": "API", "pdf": "PDF", "html": "HTML",
    "usb": "USB", "ups": "UPS", "cpu": "CPU", "os": "OS", "id": "ID",
    "aria": "ARIA", "dom": "DOM", "wav": "WAV", "mp3": "MP3", "mp4": "MP4",
}

#: "OmniTranslate - High-speed accessible translation add-on", "TextInfo:
#: counting standard pages", "AILiveTranslate (real-time speech translation)".
#: A name followed by its tagline: the name is the part before the separator.
_NAME_THEN_TAGLINE_RE = re.compile(r"^([^:(–—]{2,40}?)\s*(?:[:(]|\s[-–—]\s)")


def leading_name_segment(value):
    """Return the name a "Name: tagline" summary opens with, or "".

    Only accepted when the leading part is itself name-shaped, so a sentence
    that merely contains a colon or a dash is not chopped into a fake title.
    """
    match = _NAME_THEN_TAGLINE_RE.match((value or "").strip())
    if not match:
        return ""
    head = match.group(1).strip(" -–—:").strip()
    if not head or len(head.split()) > 5 or looks_like_prose_name(head):
        return ""
    return head


#: Joins an English name to the add-on's own name: "System Monitor, AKA
#: Monitor del Sistema". One string, because NVDA shows a single name per row.
AKA_SEPARATOR = ", AKA "

#: Vietnamese and friends: Latin letters, but a range plain English never uses.
_LATIN_EXTENDED_ADDITIONAL_RE = re.compile(r"[Ḁ-ỿ]")

#: Function words that mark a short *name* as non-English. A name has no room
#: for a stray match, so one hit is enough -- which is exactly why the list has
#: to be words English product names never contain. "Monitor", "radio" and
#: "audio" are deliberately absent: they are ordinary English.
_NON_ENGLISH_NAME_RE = re.compile(
    r"(?i)(?:^|\s)(?:de|del|della|des|du|da|do|dos|das|la|le|les|el|los|las|"
    r"lo|il|un[ao]?|und|der|die|das|voor|het|och|för|para|por|com|con|sin|"
    r"pour|avec|sur|dans|zum|zur|mit|auf|gestor|herramientas?|ferramentas?|"
    r"lector|leitor|configuraci[oó]n|configura[cç][aã]o|acess[ií]vel|"
    r"accesible|accessibilit[eé]|sistema|arquivos?|archivos?|fichiers?|"
    r"pantalla|[ée]cran|tela|teclado|clavier|sonido|som|voz|vozes|voces)"
    r"(?:\s|$)"
)


def looks_non_english_name(value):
    """True when a display name is written in a language other than English.

    Used only to decide whether to show an English name alongside the
    original, so it errs towards saying no: a false positive appends a
    redundant "AKA" to a name that never needed one.
    """
    text = (value or "").strip()
    if not text:
        return False
    return bool(
        _NON_LATIN_SCRIPT_RE.search(text)
        or _LATIN_EXTENDED_ADDITIONAL_RE.search(text)
        or _NON_ENGLISH_NAME_RE.search(text)
    )


def compose_display_name(english, original):
    """Show the English name first, then the add-on's own name after "AKA".

    An add-on named "Gestor de BIOS y UEFI Accesible" tells an English-speaking
    user nothing about what it does, and dropping the original name entirely
    would stop them recognising it from its own documentation or from anywhere
    else it is discussed. Both, English first, is the readable order:
    "Accessible BIOS and UEFI Manager, AKA Gestor de BIOS y UEFI Accesible".
    """
    english = (english or "").strip()
    original = (original or "").strip()
    if not original or not english:
        return english or original
    if english.casefold() == original.casefold():
        return english
    # Nothing to add when the original is already inside the English name, or
    # when the original is that same name plus a tagline in its own language:
    # "SoundTub" against "SoundTub - download acessivel de audio e video" is
    # one name and a subtitle, not two names.
    if original.casefold() in english.casefold():
        return english
    if original.casefold().startswith(english.casefold()):
        return english
    # The "English" name is itself in the other language; there is no pair.
    if looks_non_english_name(english):
        return english
    if not looks_non_english_name(original):
        return english
    return f"{english}{AKA_SEPARATOR}{original}"


def best_display_name(summary, manifest_summary, addon_id):
    """Pick the most name-like of the strings a build has for one add-on.

    Order: whatever the catalog stated (so a curated English name or an
    overlay still wins), then the add-on's own manifest.ini summary, then the
    name either of them opens with before a tagline, then the add-on id made
    readable. A wrong-looking title still beats a paragraph in a list of
    names, so this never gives up and returns the sentence.
    """
    candidates = [summary, manifest_summary]
    for candidate in candidates:
        text = (candidate or "").strip()
        if text and not looks_like_prose_name(text):
            return text
    for candidate in candidates:
        head = leading_name_segment(candidate)
        if head:
            return head
    fallback = humanized_addon_id(addon_id)
    if fallback:
        return fallback
    for candidate in candidates:
        if (candidate or "").strip():
            return candidate.strip()
    return ""


def _locale_fallbacks(lang):
    """NVDA's own lookup order for a language: pt_BR, then pt.

    Mirrors addonHandler._translatedManifestPaths, so a translation an author
    filed under "pt" is used for a pt_BR reader exactly as NVDA would use it.
    """
    if "_" in lang:
        return (lang, lang.split("_")[0])
    return (lang,)


def localize_catalog(output, lang, official, authored, machine):
    """Return the catalog as one language sees it.

    Three sources, best first:

    1. the official store's own per-language view (the author's words, already
       reviewed by NV Access),
    2. locale/<lang>/manifest.ini inside the add-on bundle (the author's words
       for everything the official store does not carry),
    3. machine translation, for whatever is still English.

    Anything with no translation at any tier keeps its English string, which is
    what NVDA shows today, so a missing translation is never worse than now.
    """
    if lang == "en":
        return output
    localized = []
    for addon in output:
        row = _translation_row(addon)
        replacement = {}
        for field in TRANSLATABLE_FIELDS:
            english = addon.get(field) or ""
            if not english:
                continue
            value = ""
            for candidate in _locale_fallbacks(lang):
                value = (
                    (official.get(candidate, {}).get(row) or {}).get(field)
                    or (authored.get(row, {}).get(candidate) or {}).get(field)
                    or machine.get(translation_key(english, candidate))
                    or ""
                )
                if value:
                    break
            if value and value != english:
                replacement[field] = value
        localized.append({**addon, **replacement} if replacement else addon)
    return localized


def translation_gaps(output, lang, official, authored):
    """(lang, text) pairs this language still has no human translation for."""
    for addon in output:
        row = _translation_row(addon)
        for field in TRANSLATABLE_FIELDS:
            english = addon.get(field) or ""
            if not english:
                continue
            if any(
                (official.get(c, {}).get(row) or {}).get(field)
                or (authored.get(row, {}).get(c) or {}).get(field)
                for c in _locale_fallbacks(lang)
            ):
                continue
            yield lang, english


def _esc(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_rejected_page(rejected, stats):
    """Render an accessible, browsable page of the rejected candidates."""
    groups = {}
    for r in rejected:
        groups.setdefault(r.get("reason") or "unknown", []).append(r)

    def group_slug(reason):
        # Stable, CSS/anchor-safe id per reason.
        return re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-") or "other"

    rows = []
    toc = []
    for reason in sorted(groups, key=lambda g: (-len(groups[g]), g)):
        entries = groups[reason]
        slug = group_slug(reason)
        toc.append(
            f"<li><a href='#{slug}'>{_esc(reason)}</a> ({len(entries)})</li>"
        )
        items = []
        for e in sorted(entries, key=lambda x: (x.get("addonId") or "").lower()):
            src = e.get("source") or "unknown"
            items.append(
                "<tr>"
                f"<td>{_esc(e.get('addonId') or '(unnamed)')}</td>"
                f"<td>{_esc(src)}</td>"
                f"<td>{_esc(reason)}</td>"
                "</tr>"
            )
        rows.append(
            f"<section id='{slug}'>"
            f"<h2>{_esc(reason)} ({len(entries)})</h2>"
            "<table>"
            "<caption>Add-ons excluded for this reason</caption>"
            "<thead><tr><th scope='col'>Add-on</th>"
            "<th scope='col'>Source</th>"
            "<th scope='col'>Reason</th></tr></thead>"
            f"<tbody>{''.join(items)}</tbody>"
            "</table></section>"
        )

    total = len(rejected)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rejected candidates — NVDA Add-on Store Mirror</title>
<style>
body {{ font-family: sans-serif; max-width: 70rem; margin: 1rem auto; padding: 0 1rem; line-height: 1.5; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #999; padding: 0.25rem 0.5rem; text-align: left; vertical-align: top; }}
thead th {{ background: #eee; }}
caption {{ text-align: left; font-style: italic; padding-bottom: 0.25rem; }}
h1, h2 {{ line-height: 1.2; }}
.search {{ margin: 1rem 0; }}
.result {{ font-weight: bold; }}
</style>
</head>
<body>
<h1>Rejected candidates</h1>
<p>
{stats['accepted']} add-ons are mirrored. {total} candidates were excluded
while building this mirror. They are listed below for transparency and
browsing; they are not available in the mirror's add-on store data.
</p>
<p class="search">
<label for="filter">Filter by add-on name or reason</label>:
<input id="filter" type="search" size="40" autocomplete="off">
<span id="count" class="result" role="status" aria-live="polite"></span>
</p>
<h2>Reasons</h2>
<ul>
{''.join(toc)}
</ul>
{''.join(rows)}
<p><a href="index.html">Back to the mirror home page</a></p>
<script>
(function () {{
  var input = document.getElementById("filter");
  var count = document.getElementById("count");
  var rows = Array.prototype.slice.call(document.querySelectorAll("tbody tr"));
  function update() {{
    var q = input.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (row) {{
      var match = !q || row.textContent.toLowerCase().indexOf(q) !== -1;
      row.style.display = match ? "" : "none";
      if (match) shown++;
    }});
    count.textContent = q ? shown + " matching of " + rows.length : "";
  }}
  input.addEventListener("input", update);
}})();
</script>
</body>
</html>
"""


def emit(
    out_dir,
    canonical_bytes,
    compatible_bytes,
    cache_hash,
    api_versions,
    locales,
    channels,
    stats,
    rejected,
    hosted,
    localize,
    dump,
    back_compat_by_ver,
):
    """Write the site.

    ``localize(lang)`` returns the catalog as that language sees it and
    ``dump`` serialises a catalog the way NVDA expects; ``canonical_bytes``
    and ``compatible_bytes`` are the English renderings, kept for the
    top-level addons.json and the recorded counts.
    """
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "addons.json"), "wb") as f:
        f.write(canonical_bytes)
    with open(os.path.join(out_dir, "cacheHash.json"), "w", encoding="utf-8") as f:
        json.dump(cache_hash, f)

    with open(os.path.join(out_dir, "rejected.json"), "w", encoding="utf-8") as f:
        json.dump(rejected, f, indent=2, ensure_ascii=False)

    with open(os.path.join(out_dir, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    with open(os.path.join(out_dir, ".nojekyll"), "wb") as f:
        pass

    # Pinned variant add-ons: host the repackaged bundles ourselves, since the
    # original release asset's manifest carries the colliding add-on ID.
    if hosted:
        os.makedirs(os.path.join(out_dir, "downloads"), exist_ok=True)
        for rel, blob in hosted:
            with open(os.path.join(out_dir, rel), "wb") as f:
                f.write(blob)

    index_html = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>NVDA Add-on Mirror</title></head><body>"
        "<h1>NVDA Add-on Store Mirror</h1>"
        f"<p>{stats['accepted']} add-ons mirrored from the "
        "<a href='https://github.com/nvaccess/addon-datastore'>official NV Access "
        "add-on store</a> (with the "
        "<a href='https://github.com/nvdacn/NVDAUpdateMirror'>Chinese community "
        "mirror</a> as failover), "
        "<a href='https://bestmidi.com/addons/'>bestmidi.com/addons/</a> and "
        "<a href='https://nvda-addons.ru/'>nvda-addons.ru</a>, "
        "<a href='https://nvda.es/'>nvda.es</a> (with "
        "<a href='https://nvda-addons.org/'>nvda-addons.org</a> failover), and "
        "validated direct releases from the configured GitHub authors.</p>"
        "<p>Set the NVDA Add-on Store base URL to this site to use it. "
        "Community add-ons are untested; install at your own risk. Official "
        "store add-ons include VirusTotal scan results.</p>"
        f"<p><a href='rejected.html'>{len(rejected)} rejected candidates</a> "
        "are listed on a separate page.</p>"
        "</body></html>"
    )
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

    with open(os.path.join(out_dir, "rejected.html"), "w", encoding="utf-8") as f:
        f.write(build_rejected_page(rejected, stats))

    def write_bytes(rel_path, data):
        # GitHub Pages rejects artifacts containing symlinks, and the artifact
        # upload follows them anyway (ballooning size), so always write real
        # copies rather than symlinks.
        target = os.path.join(out_dir, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)

    # Each locale is rendered, written and released before the next one starts.
    # Holding all seventy-four in memory at once would cost most of a gigabyte
    # for no benefit, since nothing needs two locales at the same time.
    for lang in locales:
        localized = localize(lang)
        lang_canonical = dump(localized)
        for channel in channels:
            # "latest.json" is NVDA's "include incompatible add-ons" view: the
            # full catalog. The per-apiVersion files are the "compatible" view
            # and must only contain add-ons compatible with that API version.
            write_bytes(f"{lang}/{channel}/latest.json", lang_canonical)
            for ver in api_versions:
                if ver == "latest":
                    continue
                write_bytes(
                    f"{lang}/{channel}/{ver}.json",
                    dump(_compatible_for_api_version(
                        localized, parse_api_version(ver) or (0, 0, 0),
                        back_compat_by_ver[ver],
                    )),
                )


_AUDIT_REQUIRED_KEYS = (
    "addonId", "displayName", "description", "publisher", "channel",
    "addonVersionName", "addonVersionNumber", "license", "sourceURL", "URL",
    "sha256", "minNVDAVersion", "lastTestedVersion",
)
_AUDIT_CHANNELS = {"stable", "beta", "dev"}


def audit_catalog(out_dir, api_version_path=NVDA_API_VERSIONS_PATH):
    """Return a list of problems found in a built mirror output.

    Every numbered ``{lang}/all/{apiVersion}.json`` is the "compatible" view
    NVDA serves to the release running that API version, and NVDA trusts the
    server's filter rather than re-checking it. A regression here would
    silently empty or pollute a release's compatible list, so each file must
    contain only add-ons where ``minimumNVDAVersion <= apiVersion`` and
    ``lastTestedNVDAVersion >= that release's BACK_COMPAT_TO``, must have no
    duplicate ``(addonId, channel)`` rows, and every entry must carry the keys
    the oldest supported client (NVDA 2025.1) requires. ``latest.json`` is the
    unfiltered "show all" view and is not audited.

    BACK_COMPAT_TO values come from the build's own ``stats.json`` (recorded
    from the live addon-datastore metadata, with the bundled
    ``nvdaAPIVersions.json`` as fallback), so the check needs no network and
    cannot drift from what the build actually used.
    """
    problems = []
    stats_path = os.path.join(out_dir, "stats.json")
    if not os.path.exists(stats_path):
        return [f"{stats_path} missing; cannot audit"]
    with open(stats_path, "r", encoding="utf-8") as f:
        stats = json.load(f)
    floors = {
        str(ver): tuple(int(part) for part in back)
        for ver, back in (stats.get("back_compat_to") or {}).items()
    }
    bundled_floors = load_nvda_api_versions(api_version_path)
    counts = stats.get("compatible_counts") or {}

    for path in sorted(glob.glob(os.path.join(out_dir, "*", "all", "*.json"))):
        ver_name = os.path.splitext(os.path.basename(path))[0]
        if ver_name == "latest":
            continue
        ver_tuple = parse_api_version(ver_name)
        if ver_tuple is None:
            problems.append(f"{path}: {ver_name!r} is not a valid API version file name")
            continue
        floor = floors.get(ver_name) or bundled_floors.get(ver_name)
        if floor is None:
            problems.append(f"{path}: no BACK_COMPAT_TO recorded for {ver_name}")
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path}: unreadable: {exc}")
            continue
        if not isinstance(entries, list):
            problems.append(
                f"{path}: expected a JSON list, got {type(entries).__name__}"
            )
            continue
        seen = set()
        for index, addon in enumerate(entries):
            label = f"{path}[{index}]:"
            for key in _AUDIT_REQUIRED_KEYS:
                if key not in addon:
                    problems.append(f"{label} missing required key {key!r}")
            channel = addon.get("channel")
            if channel not in _AUDIT_CHANNELS:
                problems.append(f"{label} unexpected channel {channel!r}")
            for key in ("minNVDAVersion", "lastTestedVersion", "addonVersionNumber"):
                version_dict = addon.get(key)
                try:
                    parsed = (
                        int(version_dict["major"]),
                        int(version_dict["minor"]),
                        int(version_dict["patch"]),
                    )
                except (KeyError, TypeError, ValueError):
                    problems.append(f"{label} {key} is not a major/minor/patch dict")
                    continue
                if key == "minNVDAVersion" and parsed > ver_tuple:
                    problems.append(
                        f"{label} minimumNVDAVersion {parsed} is newer than {ver_name}"
                    )
                if key == "lastTestedVersion" and parsed < floor:
                    problems.append(
                        f"{label} lastTestedVersion {parsed} is below "
                        f"{ver_name}'s BACK_COMPAT_TO {floor}"
                    )
            row = (str(addon.get("addonId", "")).casefold(), channel)
            if row in seen:
                problems.append(f"{label} duplicate (addonId, channel) row {row!r}")
            seen.add(row)
        expected = counts.get(ver_name)
        if expected is not None and len(entries) != expected:
            problems.append(
                f"{path}: {len(entries)} entries, stats.json records {expected}"
            )
    return problems


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build a NVDA add-on store mirror.")
    parser.add_argument("--out", default="public")
    parser.add_argument("--sources", default=",".join(ALL_SOURCES),
                        help=("comma-separated sources: official,bestmidi,ru,es,"
                              "github_owner,pinned"))
    parser.add_argument("--locales", help="comma-separated locale override")
    parser.add_argument("--api-versions", help="comma-separated apiVersion override")
    parser.add_argument(
        "--api-version-years", type=int, default=API_VERSION_YEARS_IN_FULL,
        help=("publish every release line from this many recent NVDA years, "
              "plus the newest line of each older year; 0 keeps every "
              "line. Each line costs roughly 120 MB across all locales."),
    )
    parser.add_argument("--channels", help="comma-separated channel override")
    parser.add_argument(
        "--no-translate", dest="translate", action="store_false",
        help=("publish every locale in English instead of translating it. "
              "Skips the per-language official-store fetches; the author "
              "translations already in the download cache are ignored too."),
    )
    parser.add_argument("--limit", type=int, default=0, help="process only first N add-ons (testing)")
    parser.add_argument("--skip-download", action="store_true", help="fill dummy sha256 (testing)")
    parser.add_argument("--no-head-check", action="store_true",
                        help="always re-download, never trust cached hashes")
    parser.add_argument("--hashcache", default="hashcache.json",
                        help="path to persistent sha256 cache")
    parser.add_argument("--site-base-url",
                        default="https://serrebidev.github.io/nvda-addon-mirror",
                        help="public base URL of the mirror (used for hosted pinned add-ons)")
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()

    sources = args.sources.split(",")
    locales = args.locales.split(",") if args.locales else LOCALES
    channels = args.channels.split(",") if args.channels else CHANNELS

    global TRANSLATIONS
    TRANSLATIONS = load_translations()

    nvda_api_entries = load_nvda_api_version_entries(refresh=True)
    nvda_api_versions = nvda_api_versions_from_entries(nvda_api_entries)
    if args.api_versions:
        api_versions = args.api_versions.split(",")
    else:
        api_versions = published_nvda_api_versions(
            nvda_api_entries, years_in_full=args.api_version_years
        ) + ["latest"]
        current = current_nvda_api_version()
        if current and current not in api_versions:
            api_versions.insert(0, current)
    api_versions = list(dict.fromkeys(api_versions))
    _log_api_version_retention(nvda_api_entries, api_versions)

    hashcache = load_hashcache(args.hashcache)
    new_hashcache = dict(hashcache)

    # 1. Fetch and normalize from each source.
    #
    # The four catalog sources below are polled on SOURCE_POLL_SECONDS rather
    # than read on every build: they are ~15 MB of unconditional downloads from
    # four other people's servers, and none of them supports a cheap freshness
    # check. The GitHub sources that follow cost one conditional request per
    # repository -- a 304 that GitHub does not even charge to the rate limit --
    # so those do refresh on every build. This keeps the hourly mirror from
    # repeatedly pulling unchanged catalog bodies from other people's servers.
    source_cache = load_json_cache(SOURCE_CACHE_PATH)
    # --no-head-check already means "trust nothing that was cached"; a human
    # who asks for that is asking for the catalogs too, not only the hashes.
    source_ttl = 0 if args.no_head_check else SOURCE_POLL_SECONDS
    all_entries = []
    rejected = []

    if "official" in sources:
        log("Fetching official NV Access add-on store (Chinese mirror failover)")
        entries = polled_source("official", source_cache, fetch_official, ttl=source_ttl)
        log(f"official: {len(entries)} add-ons")
        all_entries.extend(entries)

    if "bestmidi" in sources:
        log(f"Fetching {BESTMIDI_URL}")
        entries = polled_source("bestmidi", source_cache, fetch_bestmidi, ttl=source_ttl)
        log(f"bestmidi: {len(entries)} add-ons")
        all_entries.extend(entries)

    if "ru" in sources:
        log(f"Fetching {RU_ADDONS_URL}")
        entries = polled_source("ru", source_cache, fetch_ru, ttl=source_ttl)
        log(f"nvda-addons.ru: {len(entries)} add-ons")
        all_entries.extend(entries)

    if "es" in sources:
        log("Fetching nvda.es add-on catalog (nvda-addons.org failover)")
        entries = polled_source("es", source_cache, fetch_es, ttl=source_ttl)
        log(f"Spanish catalog: {len(entries)} add-on/channel candidates")
        all_entries.extend(entries)

    if "github_owner" in sources:
        log("Fetching configured GitHub authors and direct add-on artifacts")
        entries = fetch_github_owners(existing_entries=all_entries)
        log(f"GitHub authors: {len(entries)} add-on/channel candidates")
        all_entries.extend(entries)
        rejected.extend(GITHUB_OWNER_REJECTIONS)

    if "pinned" in sources:
        log("Fetching pinned variant add-ons")
        entries = fetch_pinned()
        log(f"pinned: {len(entries)} add-ons")
        all_entries.extend(entries)

    global ENGLISH_CHANGELOGS
    ENGLISH_CHANGELOGS = english_changelogs(all_entries)

    if args.limit:
        all_entries = all_entries[: args.limit]

    # 1b. Recover versions the catalogs failed to state, while it is still
    # free to do so. Running before dedupe means the merge and the
    # channel-duplicate check compare real releases rather than 0.0.0.
    recovered = recover_uninformative_versions(all_entries)
    if recovered:
        log(f"Recovered {recovered} versions from add-on file names")

    # 2. Filter.
    todo = []
    for e in all_entries:
        reason = reject_reason(e)
        if reason:
            rejected.append({"addonId": e.get("name"), "source": e.get("source"),
                             "reason": reason})
            continue
        todo.append(e)
    log(f"After filter: {len(todo)} accepted, {len(rejected)} rejected")

    # Only valid stronger-source entries suppress Spanish aliases. Matching is
    # channel-specific: a dev-only Russian entry must not hide a stable Spanish
    # release of the same add-on.
    if "es" in sources:
        before_es = sum(1 for entry in todo if entry.get("source") == "es")
        todo = keep_original_es_entries(todo)
        original_es = sum(1 for entry in todo if entry.get("source") == "es")
        log(
            f"Spanish catalog originals: {original_es}; "
            f"already covered by valid stronger sources in the same channel: "
            f"{before_es - original_es}"
        )

    # 2b. Drop community-source entries superseded by a pinned variant. These
    # share the generic manifest name of a pinned add-on (e.g. the four
    # "Eloquence" variants all publish name = Eloquence), so they would appear
    # as duplicates alongside the distinctly-named pinned entries.
    excluded_names = {
        (spec.get("name") or "").strip()
        for spec in _load_excluded(PINNED_CONFIG_PATH)
    }
    if excluded_names:
        kept = []
        excluded_count = 0
        for e in todo:
            if e.get("source") in ("ru", "bestmidi") and e.get("name") in excluded_names:
                rejected.append({
                    "addonId": e.get("name"),
                    "source": e.get("source"),
                    "reason": "excluded (pinned variant replaces it)",
                })
                excluded_count += 1
                continue
            kept.append(e)
        todo = kept
        if excluded_count:
            log(f"Excluded {excluded_count} community entries replaced by pinned variants")

    # 3. Dedupe across sources.
    todo = dedupe(todo)
    log(f"After dedupe: {len(todo)} unique add-ons")

    # 3b. One add-on, one row: drop a dev or beta entry whose version is the
    # release the stable channel already carries.
    todo, channel_duplicates = drop_redundant_channel_duplicates(todo)
    if channel_duplicates:
        rejected.extend(channel_duplicates)
        log(
            f"Dropped {len(channel_duplicates)} pre-release entries identical "
            "to their stable release"
        )

    # 4. Download + hash (with persistent, resumable cache).
    cache_lock = threading.Lock()
    download_locks = {e["download_url"]: threading.Lock() for e in todo}
    completed_count = 0
    FLUSH_EVERY = 25

    def flush_cache():
        if not args.hashcache:
            return
        os.makedirs(os.path.dirname(args.hashcache) or ".", exist_ok=True)
        tmp = args.hashcache + ".tmp"
        with cache_lock:
            snapshot = dict(new_hashcache)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, args.hashcache)

    def hash_one(e):
        url = e["download_url"]
        # Pinned variants: we host the repackaged bundle ourselves, so the
        # hash is already computed and the bytes are already in memory.
        # A pinned bundle we repackaged is one we must serve ourselves, since
        # the renamed manifest no longer matches the author's file. A pinned
        # URL we left untouched keeps pointing at the author's own download.
        if e.get("source") == "pinned" and "_patched_bytes" in e:
            patched = e.pop("_patched_bytes")
            e["_hosted"] = True
            rel = f"downloads/{e['name']}-{e['version']}.nvda-addon"
            with cache_lock:
                hosted.append((rel, patched))
                new_hashcache[url] = {
                    "sha256": e["sha256"], "size": len(patched),
                }
            return e, e["sha256"], len(patched), None

        # Official store entries ship an upstream sha256 -- nothing to download.
        if e.get("sha256"):
            return e, e["sha256"], 0, None

        if args.skip_download:
            # Testing only. Deliberately NOT written to the persistent
            # cache: a local smoke run must not leave invented digests
            # behind for the next real build to publish as verified.
            return e, hashlib.sha256(url.encode()).hexdigest(), 0, None

        with download_locks[url]:
            # A catalog that states no usable version ("unknown", "current",
            # a bare "0") still ships the real one in manifest.ini, and these
            # bytes are being streamed anyway, so read it out of this download
            # rather than publish the add-on as 0.0.0. The file name was
            # already tried, for free, before the filter.
            needs_version = version_is_uninformative(e.get("version"))
            cached, error = cached_download(
                e, new_hashcache.get(url), force=args.no_head_check,
                # Always capture. The bytes are being streamed for the sha256
                # either way, and holding them lets this build read both the
                # real version and the author's own translations without ever
                # asking the host for the file a second time.
                capture_limit=MANIFEST_CAPTURE_LIMIT,
                inspect=needs_version,
            )
            with cache_lock:
                new_hashcache[url] = cached
            replacement = better_version(
                e.get("version"), cached.get("manifest_version"),
            ) if needs_version else None
            if replacement:
                log(f"{e.get('name')}: version {e.get('version')!r} -> "
                    f"{replacement!r} from manifest.ini")
                e["version"] = replacement
            # The add-on's own name, for a catalog that stated a description
            # in place of one. transform() only reaches for it when what the
            # catalog gave is a sentence.
            if cached.get("manifest_summary"):
                e.setdefault("manifest_summary", cached["manifest_summary"])
            return e, cached.get("sha256"), cached.get("size", 0), error

    results = []
    download_failed = []
    hosted = []  # (relative path, bytes) for pinned add-ons we host ourselves
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(hash_one, e): e for e in todo}
        for fut in concurrent.futures.as_completed(futures):
            e, digest, size, err = fut.result()
            if err or digest is None:
                download_failed.append(
                    {"addonId": e.get("name"), "source": e.get("source"),
                     "reason": f"download failed: {err}"}
                )
            else:
                results.append((e, digest, size))
            completed_count += 1
            if completed_count % FLUSH_EVERY == 0:
                flush_cache()
    flush_cache()

    log(f"Hashed {len(results)}, download-failed {len(download_failed)}")

    # Rewrite pinned variants' download URL to the hosted copy (their original
    # release asset carries the colliding manifest name).
    base = args.site_base_url.rstrip("/")
    for e, digest, size in results:
        if e.get("_hosted"):
            e["download_url"] = f"{base}/downloads/{e['name']}-{e['version']}.nvda-addon"

    # 5. Transform.
    output = []
    for e, digest, size in results:
        output.append(transform(e, digest))

    rejected.extend(download_failed)
    rejected.sort(key=lambda r: (r.get("source") or "", r.get("addonId") or ""))

    output.sort(key=lambda o: (o["addonId"], o["channel"]))

    def dump(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    canonical_bytes = dump(output)

    # NVDA's "compatible" endpoint ({apiVersion}.json) must contain only add-ons
    # compatible with that API version (minimumNVDAVersion <= apiVersion and
    # lastTestedNVDAVersion >= BACK_COMPAT_TO). The "latest.json" endpoint keeps
    # the full catalog and backs the "include incompatible add-ons" toggle.
    #
    # BACK_COMPAT_TO is per-version: it rose over time, so a single master value
    # would shrink older releases' compatible lists. Live addon-datastore data,
    # with the bundled nvdaAPIVersions.json as an offline fallback, supplies each
    # released version's value. NVDA master is the fallback for an unlisted dev
    # build.
    master_back_compat_to = back_compat_to_version()
    compatible_bytes = {}
    back_compat_by_ver = {}
    for ver in api_versions:
        if ver == "latest":
            continue
        ver_tuple = parse_api_version(ver) or (0, 0, 0)
        back_compat_to = nvda_api_versions.get(ver, master_back_compat_to)
        back_compat_by_ver[ver] = back_compat_to
        compatible_bytes[ver] = dump(
            _compatible_for_api_version(output, ver_tuple, back_compat_to)
        )
    log(f"compatible counts (BACK_COMPAT_TO per version): "
        f"{ {v: len(json.loads(b)) for v, b in compatible_bytes.items()} }")

    # ---- Translations -------------------------------------------------
    # English is the source catalog; every other locale is the same catalog
    # with translated prose overlaid. Best source wins: the official store's
    # own per-language view, then the author's locale/<lang>/manifest.ini out
    # of the bundle, then machine translation for what is left.
    locale_cache = load_json_cache(LOCALE_CACHE_PATH)
    translation_cache = load_json_cache(TRANSLATION_CACHE_PATH)
    official_translations = {}
    if args.translate:
        official_translations = official_store_translations(
            locales, locale_cache, ttl=0 if args.no_head_check else None,
        )

    authored_translations = {}
    for e, _digest, _size in results:
        cached = new_hashcache.get(e.get("download_url") or "")
        locales_from_bundle = (cached or {}).get("manifest_locales")
        if locales_from_bundle:
            authored_translations[_translation_row(e)] = locales_from_bundle
    if authored_translations:
        log(f"author-supplied bundle translations: "
            f"{len(authored_translations)} add-ons")

    if args.translate and TRANSLATE_API_KEY:
        gaps = []
        for lang in locales:
            if lang == "en":
                continue
            gaps.extend(translation_gaps(
                output, lang, official_translations, authored_translations,
            ))
        spent = machine_translate_missing(gaps, translation_cache)
        if spent:
            log(f"machine translation: {spent} characters sent this build")
    elif args.translate:
        log("machine translation disabled (TRANSLATE_API_KEY unset); "
            "untranslated add-ons stay in English")

    def localize(lang):
        return localize_catalog(
            output, lang, official_translations, authored_translations,
            translation_cache,
        )

    # Hash the canonical catalog plus every compatibility-filtered view, so any
    # change to the catalog OR the filter bumps the hash and forces NVDA clients
    # to re-fetch the affected endpoint. Translations are part of what a client
    # holds, so a language changing has to move the hash too -- NVDA fetches a
    # single global cacheHash.json, not one per language.
    hasher = hashlib.sha256()
    hasher.update(canonical_bytes)
    for ver in api_versions:
        if ver == "latest":
            continue
        hasher.update(b"\x00" + compatible_bytes[ver])
    for lang in sorted(locales):
        if lang == "en":
            continue
        for addon_id, fields in sorted(
            (official_translations.get(lang) or {}).items()
        ):
            hasher.update(f"\x00{lang}:{addon_id}:{sorted(fields.items())}".encode())
    for addon_id, langs in sorted(authored_translations.items()):
        hasher.update(f"\x00{addon_id}:{sorted(langs)}".encode())
    hasher.update(f"\x00mt:{len(translation_cache)}".encode())
    cache_hash = hasher.hexdigest()

    stats = {
        "accepted": len(output),
        "rejected": len(rejected),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api_versions": api_versions,
        "back_compat_to": back_compat_by_ver,
        "compatible_counts": {v: len(json.loads(b)) for v, b in compatible_bytes.items()},
        "locales": len(locales),
        "translations": {
            "official_strings": sum(
                len(t) for t in official_translations.values()
            ),
            "authored_addons": len(authored_translations),
            "machine_strings": sum(1 for v in translation_cache.values() if v),
        },
        "sources": sources,
    }

    emit(
        args.out,
        canonical_bytes,
        compatible_bytes,
        cache_hash,
        api_versions,
        locales,
        channels,
        stats,
        rejected,
        hosted,
        localize,
        dump,
        back_compat_by_ver,
    )

    # Persist the hash cache so the next run only re-downloads changed add-ons.
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "hashcache.json"), "w", encoding="utf-8") as f:
        json.dump(new_hashcache, f)
    if os.path.exists(GITHUB_OWNER_CACHE_PATH):
        with open(GITHUB_OWNER_CACHE_PATH, "rb") as owner_cache_file:
            with open(os.path.join(args.out, GITHUB_OWNER_CACHE_PATH), "wb") as public_cache:
                public_cache.write(owner_cache_file.read())
    # Publish the merged API-version history the same way as the hash cache, so
    # the next run restores it instead of falling back to whatever was last
    # committed. The bundled copy only has to survive a cold start; without this
    # it silently loses a year of API versions per year, and an unlisted version
    # falls back to FALLBACK_BACK_COMPAT_TO.
    with open(os.path.join(args.out, NVDA_API_VERSIONS_PATH), "w", encoding="utf-8") as f:
        # Tabs, matching nvaccess/addon-datastore and the committed copy, so a
        # restored file does not show up as a whole-file reformat.
        json.dump(nvda_api_entries, f, indent="	", ensure_ascii=False)
    # Translation caches ride along with the site the same way. The locale
    # cache holds each language's ETag so an unchanged language costs a 304
    # instead of 1.7 MB; the translation cache holds machine translations, so
    # text that has not changed is never paid for twice.
    for cache_path, cache_data in (
        (LOCALE_CACHE_PATH, locale_cache),
        (TRANSLATION_CACHE_PATH, translation_cache),
    ):
        with open(os.path.join(args.out, cache_path), "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False)
    # The polled catalogs are the one cache that is deliberately NOT published
    # with the site. It is several megabytes of upstream entries that no client
    # reads, and losing it costs exactly one ordinary poll of each source --
    # which is what a build did every time before it existed. The workflow
    # cache carries it between runs; a cold start simply polls.
    with open(SOURCE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(source_cache, f, ensure_ascii=False)
    flush_cache()

    # Measure the finished output, then rewrite stats.json with the figure in
    # it. Everything above is already on disk, so this is the real number
    # rather than a projection.
    _report_site_size(args.out, stats)
    with open(os.path.join(args.out, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    total_bytes = sum(s for _, _, s in results)
    log(
        f"Done: {stats['accepted']} add-ons, {total_bytes/1024/1024:.1f} MiB hashed, "
        f"cacheHash={cache_hash}"
    )
    if rejected:
        log(f"Rejected {len(rejected)} candidates (see {args.out}/rejected.json)")


if __name__ == "__main__":
    main()
