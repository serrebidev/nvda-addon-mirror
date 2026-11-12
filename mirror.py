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
- global GitHub repository search for "nvda", most recently updated first;
  matches join the same pending queue and `.nvda-addon` release validation
  as owner-discovered repositories, so the mirror no longer depends on an
  external crawler to notice brand-new add-on repositories

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
import http.client
import io
import json
import os
import re
import threading
import time
import zipfile
import zlib
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

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
# published site small, and every path below is a real copy rather than a
# symlink, so the output deploys to any static host unchanged.
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
#: so publishing every line since 2025.1 outgrows the site's size budget.
#:
#: Retention follows where users actually are. The current and previous years
#: keep every line, because that is where nearly everyone sits and a missing
#: file is an empty Add-on Store, not a degraded one -- NVDA has no version
#: fallback. Older years keep only their newest line, on the assumption that
#: anyone that far behind who is still updating at all has taken their year's
#: last patch. "Every line" includes every patch in it: NVDA requests its exact
#: year.major.minor, updating is the user's choice, and NV Access lists a patch
#: before it ships -- 2026.3.1 replaced 2026.3.0 here while still a release
#: candidate, which broke the Add-on Store for everyone on stable NVDA.
API_VERSION_YEARS_IN_FULL = 2

#: Warn when the built site passes this. The static host holds about 1 GB
#: comfortably; going over invites slow deploys and long client fetches, so
#: the build says so rather than letting it be a surprise.
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
    "Mozilla/5.0 (compatible; nvda-addon-mirror/1.0; +https://git.serrebiradio.com/"
    "serrebi/nvda-addon-mirror)"
)

ALL_SOURCES = ("official", "bestmidi", "ru", "es", "github_owner", "forgejo_owner", "pinned")

#: How much each source is trusted when two of them describe the same add-on.
#: Used both to pick the winning entry (see dedupe) and to pick which source's
#: English release notes to borrow (see english_changelogs). Forgejo outranks
#: GitHub: an owner who publishes on both means the Forgejo release to win.
SOURCE_PRIORITY = {
    "pinned": 6,
    "forgejo_owner": 5,
    "github_owner": 4,
    "official": 3,
    "ru": 2,
    "bestmidi": 1,
    "es": 0,
}
PINNED_CONFIG_PATH = "pinned.json"
BLOCKED_CONFIG_PATH = "blocked.json"
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
#: Longest one package download may take. The per-read socket timeout only
#: fires on a fully idle connection, so a host dripping bytes keeps a worker
#: forever -- three throttled 150+ MB bundles once held every build past the
#: service timeout, and the next run retried the same files. Past this the
#: download fails into the ordinary 6h backoff like any other slow host.
DOWNLOAD_MAX_SECONDS = 10 * 60
#: Download locations that no longer exist. Official entries carry their own
#: hash, so nothing ever tests their URL, and they outrank a live copy of the
#: same version. The integrumart GitHub account was deleted, which left 14
#: published add-ons that could not be installed although nvda.ru hosts them.
# ponytail: hand-kept list, automate a repo check if more accounts disappear.
DEAD_DOWNLOAD_PREFIXES = ("https://github.com/integrumart/",)
#: How long an add-on stays published on its last verified hash while its host
#: cannot be reached. nvda.ru times out from some GitHub runners while its files
#: are fine, and rejecting on that dropped up to 214 working add-ons per build.
#: The limit stops a host that has really gone from staying listed forever.
DOWNLOAD_OUTAGE_GRACE_SECONDS = 7 * 24 * 60 * 60
#: Authors often replace a bundle under the same name within hours of the
#: first upload (the mirror recorded NeuralVoiceManager 2026.10.10, then the
#: author replaced the file two and a half hours later and NVDA rejected the
#: stale sha256 for a day). The cached Last-Modified dates the upload, so an
#: entry younger than FRESH_UPLOAD_SECONDS gets a conditional recheck every
#: FRESH_RECHECK_SECONDS instead of waiting out the 24h TTL. The last check
#: is derived from next_check, so entries written before this rule get the
#: fast recheck with no cache migration.
FRESH_UPLOAD_SECONDS = 24 * 60 * 60
FRESH_RECHECK_SECONDS = 60 * 60
#: A catalog that publishes a free-form version ("unknown", "current") still
#: ships the real one inside the bundle's manifest.ini. The hashing pass
#: already streams those bytes, so the manifest is read from the stream it
#: keeps rather than from a second request. Bundles above this size are hashed
#: without buffering: only read_manifest_name_by_range() can see inside those.
MANIFEST_CAPTURE_LIMIT = 32 * 1024 * 1024
#: One range request reads the zip directory at a bundle's tail, a second reads
#: manifest.ini. The cap only stops a pathological zip from costing more.
MANIFEST_RANGE_BLOCK = 64 * 1024
MANIFEST_RANGE_MAX_REQUESTS = 6
PINNED_BUNDLE_CACHE_PATH = ".bundlecache"

# Newly discovered repositories cost one uncached request each, while repeat
# checks are conditional and free. Adding an author with hundreds of repos would
# otherwise spend the whole hourly budget in a single run, so first-time checks
# are rationed and the remainder is carried in the cache for the next run.
GITHUB_NEW_REPOSITORY_BUDGET = 120
# The owner's own gh token (5000 requests an hour, back 2026-11-12) drains a
# multi-thousand-repo backlog in hours instead of a day. A quota hit mid-batch
# re-queues the rest, so overshooting costs nothing but a retry next run.
GITHUB_NEW_REPOSITORY_BUDGET_AUTHENTICATED = 480
# Owner rule (2026-11-12, after the suspension): stay comfortably under every
# GitHub limit. First-time checks only spend hourly quota above this reserve
# (half the 5000 an hour), and API calls are spaced so a 4-thread burst stays
# far below the 900-a-minute secondary limit.
GITHUB_QUOTA_RESERVE = 2500
GITHUB_MIN_REQUEST_INTERVAL = 0.2
#: Global search discovery (Brandon, 2026-10-16): once a day, search GitHub
#: repositories for the query below, most recently updated first, and queue
#: matches that are not already scanned, queued or catalogued. Queuing only
#: earns a repository the ordinary first-time check -- it is catalogued
#: solely when a release actually ships a `.nvda-addon` asset. A checked
#: repository is remembered by its `updated_at` stamp and is not queued
#: again until the stamp advances, so the search costs a few requests a
#: day and never re-checks the same noise every build.
GITHUB_SEARCH_DISCOVERY_QUERY = "nvda"
GITHUB_SEARCH_DISCOVERY_TTL_SECONDS = 24 * 60 * 60
GITHUB_SEARCH_DISCOVERY_PAGES = 2
GITHUB_SEARCH_DISCOVERY_BUDGET = 100
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
    "forgejo_owner": "Forgejo author release",
    "ru": "NVDA Add-ons RU",
    "bestmidi": "BestMidi",
    "es": "NVDA.es",
}
#: A pinned entry is one source for priority and dedupe, but it reaches the
#: mirror two ways. This labels the half that came from an author's own site,
#: so the helper add-on does not describe it as a GitHub release. Deliberately
#: not a STORE_SOURCE_LABELS key: it is a provenance label, not a source.
PINNED_URL_SOURCE_LABEL = "Author's website"
#: A pinned entry can also name a Forgejo release ("host": "forgejo"), which
#: needs its own provenance label for the same reason.
PINNED_FORGEJO_SOURCE_LABEL = "Pinned Forgejo release"
GITHUB_API = "https://api.github.com"
#: The owner's Forgejo instance, which serves GitHub-shaped release JSON at
#: /api/v1/repos/{owner}/{repo}/releases. Public releases need no token.
FORGEJO_WEB = "https://git.serrebiradio.com"
FORGEJO_API = FORGEJO_WEB + "/api/v1"
#: Public base URL of the published mirror. NVDA fetches
#: {base}/{lang}/{channel}/{apiVersion}.json from here, and hosted pinned
#: bundles are rewritten under {base}/downloads/.
DEFAULT_SITE_BASE_URL = "https://nvda.serrebiradio.com"
#: Helper add-on release the site's home page offers for download. Bump with
#: helper/manifest.ini; the tagged Forgejo release carries the asset.
HELPER_VERSION = "1.6.1"
HELPER_RELEASES_URL = (
    "https://git.serrebiradio.com/serrebi/nvda-addon-mirror/releases"
)
HELPER_DOWNLOAD_URL = (
    "https://git.serrebiradio.com/serrebi/nvda-addon-mirror"
    f"/releases/download/v{HELPER_VERSION}/"
    f"addonStoreMirror-{HELPER_VERSION}.nvda-addon"
)
FORGEJO_OWNERS_PATH = "forgejoOwners.json"
#: Section of the shared author cache file holding the Forgejo entries and
#: release state. One file keeps the existing workflow cache steps and the
#: published-site restore working with no new paths or secrets.
FORGEJO_CACHE_SECTION = "__forgejo__"
# urllib wraps most network faults in URLError, but a connection GitHub drops
# mid-response escapes as a bare http.client.RemoteDisconnected from
# getresponse() ("Remote end closed connection without response"), which is
# not an HTTPError. Every retry loop below reads only, so retrying these is
# safe, and a failure that outlives the backoff still stops publication.
GITHUB_TRANSIENT_ERRORS = (
    ConnectionError,
    TimeoutError,
    http.client.HTTPException,
    URLError,
)
#: The Forgejo source retries the same network faults as the GitHub one.
FORGEJO_TRANSIENT_ERRORS = GITHUB_TRANSIENT_ERRORS
GITHUB_OWNER_REJECTIONS = []
FORGEJO_OWNER_REJECTIONS = []

# GitHub API token, when present (e.g. GITHUB_TOKEN in Actions). Raises the
# api.github.com rate limit from 60 to 1000 requests/hour, which matters when
# the mirror rebuilds more often than the upstream catalogs change.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Forgejo API token, when present. Public releases need none; a token only
# matters for private repositories or a rate-limited instance.
FORGEJO_TOKEN = os.environ.get("FORGEJO_TOKEN", "")


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


#: Characters Windows forbids in a file name, plus control characters.
#: NVDA's store client saves a download as "<addonId>-<version>.nvda-addon",
#: so a published ID carrying one of these makes the add-on uninstallable:
#: the final rename fails with WinError 87 (seen 2026-10-23 on the BestMidi
#: entry "Access TV: Siaran TV Streaming Aksesibel").
_ILLEGAL_ID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def sanitize_addon_id(name):
    """Make a source-supplied add-on name safe to publish as the store ID.

    Sources (BestMidi especially) use the add-on's display name as its ID,
    punctuation and all. Illegal characters become "-", so the ID stays
    recognisable while forming a legal Windows file name. Applied to every
    entry at collection time, so translations, the blocklist, dedupe and the
    published catalog all key on the same sanitized ID.
    """
    cleaned = _ILLEGAL_ID_CHARS_RE.sub("-", (name or "").strip())
    # No trailing strip: the ID sits mid-file-name ("<id>-<version>.nvda-addon"),
    # so a source name ending in "." or "-" is harmless there, and stripping
    # it would orphan that add-on's translations/blocklist keys.
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned or (name or "").strip()


#: What NVDA's MajorMinorPatch parses from an installed add-on's version string.
_NVDA_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


def apply_manifest_names(entries, hashcache):
    """Republish entries under their bundle's manifest ``name``.

    NVDA keys everything about an installed add-on by the ``name`` in its
    manifest.ini -- including the compatibility override a user grants in
    the Add-on Store, which the store records under the catalog's addonId.
    When the two differ, the override never matches the downloaded bundle
    and NVDA rejects the store install as "not supported", while the same
    file installs fine by hand (seen 2026-11-01: the BestMidi Reloq-C entry
    published the preview add-on's name, "Eloquence-C-Hörtest (x64-DLL)",
    for the synth bundle named reloqCExperimental). The official store
    forbids the mismatch outright (its checkAddonId validation).

    The manifest name comes from the download cache, captured from bytes
    already streamed for the SHA-256 -- never a fresh download -- so an
    entry is corrected on the first build after its bundle was last
    fetched, and coverage fills in as the ordinary rechecks land. Official
    entries are validated upstream and pinned entries are repackaged under
    their published name, so neither is touched. Returns the rename count.
    """
    renamed = 0
    for entry in entries:
        if entry.get("source") in ("official", "pinned"):
            continue
        cached = hashcache.get(entry.get("download_url") or "")
        if not isinstance(cached, dict):
            continue
        manifest_name = (cached.get("manifest_name") or "").strip()
        if not manifest_name or manifest_name.casefold() in _TEMPLATE_NAMES:
            continue
        cleaned = sanitize_addon_id(manifest_name)
        if cleaned and cleaned != entry.get("name"):
            log(f"{entry.get('source')}: addonId {entry.get('name')!r} -> "
                f"{cleaned!r} (bundle manifest name)")
            # translations.json may still be keyed by the old ID.
            entry["catalog_name"] = entry.get("name")
            entry["name"] = cleaned
            renamed += 1
    return renamed


def apply_manifest_claims(entries, hashcache):
    """Publish the version and API versions NVDA will read from the bundle.

    Catalogs state these independently of the file they link. A row claiming
    compatibility for a bundle that does not declare it offers a plain Install
    that ends in "Add-on not supported", with no override on offer; a row
    denying it hides a working add-on; a wrong version misorders updates and
    lets dedupe pick the wrong release. Read from the download cache like
    apply_manifest_names, so this never costs a request. Returns the number of
    fields corrected.
    """
    corrected = 0
    for entry in entries:
        if entry.get("source") in ("official", "pinned"):
            continue
        cached = hashcache.get(entry.get("download_url") or "")
        if not isinstance(cached, dict):
            continue
        version = (cached.get("manifest_version") or "").strip()
        # NVDA's own parse: a version it cannot read is left to the catalog.
        if (_NVDA_VERSION_RE.fullmatch(version)
                and sanitize_version(version) != sanitize_version(entry.get("version"))):
            log(f"{entry.get('name')}: version {entry.get('version')!r} -> "
                f"{version!r} (bundle manifest)")
            entry.setdefault("catalog_version", entry.get("version"))
            entry["version"] = version
            corrected += 1
        for field, key in (("min_nvda", "manifest_min_nvda"),
                           ("last_tested", "manifest_last_tested")):
            stated = cached.get(key)
            if stated is None:
                continue
            # NVDA reads a missing or "None" API version as 0.0.0.
            real = parse_api_version(stated) or (0, 0, 0)
            # A polled catalog comes back from its JSON cache with lists.
            if real != tuple(entry.get(field) or ()):
                entry[field] = real
                corrected += 1
    return corrected


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
    started = time.monotonic()
    with urlopen(req, timeout=timeout) as resp:
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        while True:
            if time.monotonic() - started > DOWNLOAD_MAX_SECONDS:
                raise TimeoutError(
                    f"download exceeded {DOWNLOAD_MAX_SECONDS}s: {url}"
                )
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
    return _manifest_scalar(manifest_text, "version")


def _manifest_api_fields(manifest_text):
    return {
        "manifest_min_nvda": _manifest_scalar(manifest_text, "minimumNVDAVersion"),
        "manifest_last_tested": _manifest_scalar(manifest_text, "lastTestedNVDAVersion"),
    }


def bundle_manifest_api_versions(raw):
    """Return the NVDA API versions a bundle declares, as cache-record fields.

    NVDA decides whether a downloaded bundle may install from these, not from
    the catalog row, so a catalog that disagrees offers an install that fails.
    Returns {} for anything unreadable: nothing was learned about the bundle.
    """
    if not raw:
        return {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            text = archive.read("manifest.ini").decode("utf-8-sig", "replace")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return {}
    return _manifest_api_fields(text)


def bundle_manifest_name(raw):
    """Return the ``name`` declared in a bundle's manifest.ini, or "".

    The manifest name is the add-on's real identity in NVDA: an installed
    add-on is keyed by it, and the store's compatibility override is recorded
    under it. A catalog that publishes anything else as the store ID (a
    display name, another add-on's name) leaves NVDA unable to match the
    downloaded bundle to the store entry, so a store install that needs a
    compatibility override is rejected as "not supported" while the same
    file installs fine by hand.
    """
    if not raw:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            text = archive.read("manifest.ini").decode("utf-8-sig", "replace")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return ""
    return _manifest_name(text)


class _RangeRefused(Exception):
    """The host will not serve this bundle piecemeal."""


class _RangedBundle:
    """Just enough of a seekable file for zipfile to read a remote bundle."""

    def __init__(self, url, size, timeout):
        self.url = quote_url(url)
        self.size = size
        self.timeout = timeout
        self.position = 0
        self.block_start = 0
        self.block = b""
        self.requests = 0
        # The zip directory sits at the tail, so one request usually covers it.
        self._fetch(max(0, size - MANIFEST_RANGE_BLOCK), size)

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.position,
                io.SEEK_END: self.size}[whence]
        self.position = base + offset
        return self.position

    def read(self, count=-1):
        if count is None or count < 0:
            count = self.size - self.position
        end = min(self.position + count, self.size)
        if self.position >= end:
            return b""
        block_end = self.block_start + len(self.block)
        if not self.block_start <= self.position or end > block_end:
            self._fetch(self.position, end)
        data = self.block[self.position - self.block_start:end - self.block_start]
        self.position += len(data)
        return data

    def _fetch(self, start, end):
        self.requests += 1
        if self.requests > MANIFEST_RANGE_MAX_REQUESTS:
            raise _RangeRefused(f"more than {MANIFEST_RANGE_MAX_REQUESTS} requests")
        stop = min(max(end, start + MANIFEST_RANGE_BLOCK), self.size)
        req = Request(self.url, headers={
            "User-Agent": USER_AGENT, "Accept": "*/*",
            # Explicit bounds: GitHub answers a suffix range with 501.
            "Range": f"bytes={start}-{stop - 1}",
        })
        with urlopen(req, timeout=self.timeout) as resp:
            if resp.status != 206:
                # Closing unread is the point: a server that ignores ranges
                # is about to send the whole file, which is the exact abuse
                # an author complained about on 2026-09-05.
                raise _RangeRefused(f"HTTP {resp.status}")
            self.block = resp.read(stop - start)
            self.block_start = start
            # Stay on the redirect target, so the release's download counter
            # sees one request however many ranges the read takes.
            self.url = resp.geturl()


def read_manifest_by_range(url, size, timeout=30):
    """Read a bundle's manifest.ini without downloading the bundle.

    For the two cases a stream can never answer: a 304 carries no body, and a
    bundle over MANIFEST_CAPTURE_LIMIT is hashed without being kept. Returns ""
    when the answer is final (the host refuses ranges, or the file holds no
    readable manifest). Network trouble raises, so the caller can try again at
    the next recheck.
    """
    if not size:
        return ""
    try:
        with zipfile.ZipFile(_RangedBundle(url, size, timeout)) as archive:
            return archive.read("manifest.ini").decode("utf-8-sig", "replace")
    # RuntimeError: an encrypted entry, or a compression method zipfile lacks.
    except (_RangeRefused, zipfile.BadZipFile, KeyError, ValueError,
            RuntimeError, EOFError, zlib.error) as exc:
        log(f"No manifest by range ({exc}): {url}")
        return ""


def _fill_manifest_by_range(record, url):
    """Give a record the manifest facts no download has given it, at most once.

    ``manifest_last_tested`` is the marker: every completed look sets it, to
    None when the look found nothing, which is never repeated. A manifest_name
    of "" says nothing: builds of 2026-11-01 wrote it on a 304, and a body too
    large to keep still does.
    """
    if "manifest_last_tested" in record:
        return record
    try:
        text = read_manifest_by_range(url, record.get("size"))
    except (OSError, http.client.HTTPException) as exc:
        log(f"Manifest read failed, will retry at next recheck ({exc}): {url}")
        return record
    record["manifest_name"] = _manifest_name(text) or record.get("manifest_name") or None
    if text:
        record["manifest_version"] = _manifest_scalar(text, "version")
        record.update(_manifest_api_fields(text))
    else:
        record["manifest_last_tested"] = None
    return record


def read_unread_manifests(entries, hashcache, workers):
    """Read now each manifest the ordinary recheck has not reached yet.

    A wrong addonId leaves an add-on uninstallable, so it should not wait up to
    a day for that recheck. This moves the same single read forward and adds
    none: a finished read sets the marker the recheck looks for. A failed one
    sets ``manifest_read_tried``, so only the recheck retries it, never every
    build. Returns the number of URLs read.
    """
    # A pin rewrites its URL's record every build, dropping any marker, and
    # publishes the pin rather than the catalog row anyway.
    pinned_urls = {e.get("download_url") for e in entries if e.get("source") == "pinned"}
    pending = {}
    for entry in entries:
        if entry.get("source") in ("official", "pinned"):
            continue
        url = entry.get("download_url") or ""
        if url in pinned_urls:
            continue
        record = hashcache.get(url)
        if (isinstance(record, dict) and record.get("sha256") and record.get("size")
                and not record.get("error") and "failing_since" not in record
                and "manifest_last_tested" not in record
                and "manifest_read_tried" not in record):
            pending[url] = record

    def read(item):
        url, record = item
        try:
            _fill_manifest_by_range(record, url)
        # One odd file among thousands must not fail the build, which would
        # lose these reads and repeat them all on the next one.
        except Exception as exc:
            log(f"Manifest read failed ({exc!r}): {url}")
        record["manifest_read_tried"] = True

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(read, pending.items()))
    return len(pending)


def _fresh_upload_recheck_due(cached, usable, same_version, now):
    """True when a recently-uploaded file is due its hourly conditional recheck.

    The cached Last-Modified dates the upload; if the file was seen within
    FRESH_UPLOAD_SECONDS and the hourly recheck has elapsed since the last
    check (derived from next_check), the caller falls through to the
    conditional GET instead of returning the stale record. Entries from hosts
    that ignore conditional requests carry "ignores_conditional" and never
    fast-recheck: without a 304 the hourly request would be a full download,
    which is exactly the bulk re-download the authors cannot afford.
    """
    if not (usable and same_version):
        return False
    if cached.get("ignores_conditional") or "failing_since" in cached:
        return False
    if not (cached.get("etag") or cached.get("last_modified")):
        return False
    modified_ms = parse_http_date_to_ms(cached.get("last_modified"))
    if modified_ms is None or modified_ms < (now - FRESH_UPLOAD_SECONDS) * 1000:
        return False
    last_check = cached.get("next_check", 0) - DOWNLOAD_RECHECK_SECONDS
    return last_check + FRESH_RECHECK_SECONDS <= now


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
    # The catalog's own string, even once apply_manifest_claims() has corrected
    # what gets published: only a change in the catalog's claim bypasses the TTL.
    version = entry.get("catalog_version") or entry.get("version")
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
        and "failing_since" not in cached
    )
    # The catalog reports a size our hash was not taken from: the author
    # replaced the file under the same URL, and the published hash now fails
    # NVDA's checksum (Reloq-C 1.3.0, 2026-11-10). One conditional GET per
    # distinct claim, so a catalog stuck on a wrong size never polls.
    claimed_size = entry.get("catalog_size")
    size_claim_new = bool(
        usable and claimed_size and claimed_size != cached.get("size")
        and claimed_size != cached.get("size_claim_checked")
    )
    if not force and not unexamined:
        if same_version and cached.get("next_check", 0) > now:
            if size_claim_new:
                log(f"Catalog size changed, rechecking: {entry.get('download_url')}")
            elif _fresh_upload_recheck_due(cached, usable, same_version, now):
                # Recently uploaded and the hourly recheck is due: fall
                # through to the conditional GET rather than trust the record
                # for a day. A 304 costs the host nothing.
                log(f"Fresh upload recheck: {entry.get('download_url')}")
            else:
                return dict(cached), cached.get("error")
        # Seed old deployments without probing every author's files at once.
        if usable and "next_check" not in cached and (
            same_version or cached.get("version") is None
        ):
            return dict(cached, version=version,
                        next_check=now + DOWNLOAD_RECHECK_SECONDS), None
    sent_validators = usable and same_version and not force and not unexamined
    try:
        digest, size, etag, modified, body = sha256_stream(
            entry["download_url"],
            # An unexamined bundle must arrive as a body, so it is asked for
            # unconditionally; a 304 would carry no manifest to read.
            validators=(cached if sent_validators else None),
            capture_limit=capture_limit,
        )
    except HTTPError as exc:
        status = exc.code
        error = str(exc)
        exc.close()
        host_unavailable = status in (408, 429) or status >= 500
        if status == 304 and usable and same_version and not force:
            refreshed = dict(cached, next_check=now + DOWNLOAD_RECHECK_SECONDS)
            refreshed.pop("error", None)
            refreshed.pop("failing_since", None)
            if capture_limit:
                refreshed.setdefault("manifest_version", "")
                # A 304 carries no bundle and an unchanged release may never
                # send one, so an unnamed bundle is read by range instead:
                # waiting for a stream would leave its ID wrong for good.
                _fill_manifest_by_range(refreshed, entry["download_url"])
            return refreshed, None
    except (URLError, OSError) as exc:
        error = str(exc)
        host_unavailable = True
    else:
        record = {"sha256": digest, "size": size, "etag": etag,
                  "last_modified": modified, "version": version,
                  "next_check": now + DOWNLOAD_RECHECK_SECONDS}
        # Validators were sent and the host answered with a full body anyway
        # while the bytes never changed: it ignores conditional requests, so
        # a 304 will never arrive and the hourly fresh-upload recheck would
        # cost the author a full download every time. Flag it once and never
        # fast-recheck that URL again. A changed body (a real replacement)
        # is stored normally: the new Last-Modified makes it a fresh upload
        # and the next recheck is a cheap conditional request on hosts like
        # nvda.ru that honour them.
        if sent_validators and digest == cached.get("sha256"):
            record["ignores_conditional"] = True
            log(f"Host ignores conditional requests: {entry.get('download_url')}")
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
            # The bundle's manifest ``name``: the add-on's real store ID.
            # Catalogs (BestMidi especially) publish a display name -- or
            # another add-on's name, when a repo ships several -- instead.
            # Read from the same bytes; applied from the cache on a later
            # build (see apply_manifest_names), never a forced re-download.
            record["manifest_name"] = bundle_manifest_name(body)
            # What NVDA decides compatibility from once it has the file.
            record.update(bundle_manifest_api_versions(body))
            if size > capture_limit:
                _fill_manifest_by_range(record, entry["download_url"])
            elif body is not None and not record["manifest_name"]:
                # A landing page or a bundle with no name: its checksum would
                # verify, and then NVDA could not open it. The next look waits
                # for the ordinary recheck, without validators, so a 304 can
                # never publish it again.
                error = "not an NVDA add-on bundle (no manifest name)"
                log(f"{error}: {entry['download_url']}")
                return {"version": version, "error": error,
                        "next_check": now + DOWNLOAD_RECHECK_SECONDS}, error
        return record, None
    # Failing to reach a host says nothing about a file already verified for
    # this version, so it stays published, backed off, until the grace period
    # ends. Never trust a changed version, a missing file or a forced refresh.
    if host_unavailable and usable and same_version and not force:
        failing_since = cached.get("failing_since", now)
        if now - failing_since < DOWNLOAD_OUTAGE_GRACE_SECONDS:
            log(f"Keeping verified hash, host unavailable ({error}): "
                f"{entry.get('download_url')}")
            held = dict(cached, failing_since=failing_since,
                        next_check=now + DOWNLOAD_RETRY_SECONDS)
            held.pop("error", None)
            return held, None
    # Keep previous validators only for the same catalog version.
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

#: (Retired 2026-09-24: the only translation job is non-English -> English,
#: maintained by hand in translations.json. There is no machine-translation
#: provider any more, so English -> locale machine translations are no longer
#: generated, queued, or served.)

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


def _load_blocked(path):
    """Return the safety blocklist from blocked.json: add-ons never served.

    Each spec names a manifest `name` and the `reason` it is blocked.
    Unlike the pinned-variant exclusion above, a blocked add-on is dropped
    from every source, pinned included: safety outranks curation.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("blocked", [])
    except FileNotFoundError:
        return []


def drop_blocked_entries(entries, blocked):
    """Split out entries whose add-on is on the safety blocklist.

    Matching is on the manifest name, case-insensitively, so a blocked
    add-on stays blocked if a source republishes it with different
    casing. Returns (kept, rejected) like the other drop_* passes.
    """
    reasons = {
        (spec.get("name") or "").strip().casefold(): (spec.get("reason") or "")
        for spec in blocked
        if (spec.get("name") or "").strip()
    }
    if not reasons:
        return entries, []
    kept, rejected = [], []
    for e in entries:
        name = (e.get("name") or e.get("addonId") or "").strip()
        # A rename to the manifest name must not free a blocked add-on.
        if {name.casefold(), (e.get("catalog_name") or "").strip().casefold()} & set(reasons):
            rejected.append({
                "addonId": e.get("name") or e.get("addonId"),
                "source": e.get("source"),
                "reason": "blocked (safety): " + (
                    reasons.get(name.casefold())
                    or reasons.get((e.get("catalog_name") or "").strip().casefold())
                    or ""
                ),
            })
            continue
        kept.append(e)
    return kept, rejected


def drop_dead_downloads(entries, prefixes=DEAD_DOWNLOAD_PREFIXES):
    """Split out entries whose download location no longer exists.

    Runs before dedupe so another source's live copy can win. Returns
    (kept, rejected) like the other drop_* passes.
    """
    kept, rejected = [], []
    for e in entries:
        if (e.get("download_url") or "").casefold().startswith(prefixes):
            rejected.append({
                "addonId": e.get("name"),
                "source": e.get("source"),
                "reason": "download location no longer exists: "
                          + e["download_url"],
            })
            continue
        kept.append(e)
    return kept, rejected


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
            # A fork pinned beside its original: losing it for one build hides
            # only that fork, and its catalog rows are no longer excluded.
            # A pin never verified before, caught by a GitHub outage or spent
            # quota, waits the same way: its catalog rows stand in until the
            # next run verifies it.
            if not spec.get("optional") and not _github_outage(exc):
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
    host = (spec.get("host") or "github").strip().lower()
    if host == "forgejo":
        # Forgejo serves GitHub-shaped release JSON; only the base URL, the
        # page-size parameter, and the token scheme differ.
        web_base = FORGEJO_WEB
        releases_url = f"{FORGEJO_API}/repos/{repo}/releases?limit=20"
        api_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if FORGEJO_TOKEN:
            api_headers["Authorization"] = f"token {FORGEJO_TOKEN}"
    elif host == "github":
        web_base = "https://github.com"
        releases_url = f"{GITHUB_API}/repos/{repo}/releases?per_page=20"
        api_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        }
        if GITHUB_TOKEN:
            api_headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    else:
        raise RuntimeError(
            f"pinned entry {repo} has unknown host {host!r}; "
            "expected 'github' or 'forgejo'"
        )
    releases = _pinned_cached(
        f"releases {releases_url}", repo,
        lambda: json.loads(http_get(releases_url, headers=api_headers).decode("utf-8")),
    )
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
        # utf-8-sig: a BOM ahead of "name =" would hide the first line from
        # the rename below, publishing the variant under the original ID.
        manifest_text = zf.read("manifest.ini").decode("utf-8-sig")

    original_name = _manifest_name(manifest_text)
    if original_name == addon_id:
        log(f"pinned {repo}: manifest already named {addon_id}")
    summary = spec.get("summary") or _manifest_value(manifest_text, "summary") or release["name"] or addon_id
    description = _manifest_value(manifest_text, "description") or ""
    author = spec.get("publisher") or _manifest_value(manifest_text, "author") or ""
    mv = _manifest_value(manifest_text, "version")
    version = _select_pinned_version(mv, asset["name"], release["tag_name"])
    if not _pinned_fork_release_qualifies(
        spec, repo, sanitize_version(version), host=host
    ):
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
        "homepage": f"{web_base}/{repo}",
        "source_url": f"{web_base}/{repo}",
        "license": spec.get("license", "Unknown"),
        "license_url": spec.get("license_url", ""),
        "changelog": (release.get("body") or "").strip(),
        "download_url": f"{web_base}/{repo}/releases/download/{release['tag_name']}/{asset['name']}",
        "submission_ms": parse_iso8601_to_ms(release.get("published_at")),
        "min_nvda": min_nvda,
        "last_tested": last_tested,
        "source": "pinned",
        # Pre-computed: the build hashed the repackaged bundle above.
        "sha256": digest,
        "_patched_bytes": patched,
    }
    if host == "forgejo":
        # A Forgejo pin carries its own provenance label so the helper
        # add-on does not describe it as a GitHub release.
        entry["store_source_label"] = PINNED_FORGEJO_SOURCE_LABEL
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


def _pinned_fork_release_qualifies(spec, repo, fork_version, host="github"):
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
    check = (_forgejo_fork_release_qualifies if host == "forgejo"
             else _github_fork_release_qualifies)
    return _pinned_cached(
        f"fork {host} {repo} {fork_version}", repo,
        lambda: check(repo, fork_version),
    )


#: Last verified answer per pinned lookup (release list, fork verdict). Lives
#: beside the cached bundles so the server keeps it between builds.
PINNED_STATE_PATH = os.path.join(PINNED_BUNDLE_CACHE_PATH, "pinnedState.json")


def _pinned_cached(key, repo, fetch):
    """Run fetch(); on a rate limit or outage reuse its last verified answer.

    A pin is never published without having been verified once. A 404 or any
    other refusal still fails at once: only GitHub's 403/429 quota answers,
    408, 5xx and network faults fall back, as for release_state elsewhere.
    """
    try:
        value = fetch()
    except (HTTPError, *GITHUB_TRANSIENT_ERRORS) as exc:
        state = load_json_cache(PINNED_STATE_PATH)
        if not _github_outage(exc) or key not in state:
            raise
        if isinstance(exc, HTTPError):
            exc.close()
        log(f"pinned {repo}: {exc}; reusing last verified {key.split(' ', 1)[0]}")
        return state[key]
    state = load_json_cache(PINNED_STATE_PATH)
    if state.get(key) != value:
        state[key] = value
        os.makedirs(PINNED_BUNDLE_CACHE_PATH, exist_ok=True)
        with open(PINNED_STATE_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(PINNED_STATE_PATH + ".tmp", PINNED_STATE_PATH)
    return value


def _manifest_scalar(manifest_text, key):
    """A one-line manifest value the way NVDA's configobj reads it.

    Unquoted, single quotes included, and with no inline comment. Used for the
    fields NVDA compares (never for prose, which may contain a "#").
    """
    for line in manifest_text.splitlines():
        m = re.match(rf"^{key}\s*=\s*(.*)$", line.strip())
        if m:
            value = m.group(1).strip()
            quoted = re.fullmatch(r"""(["'])(.*?)\1\s*(?:#.*)?""", value)
            if quoted:
                return quoted.group(2)
            return value.split("#", 1)[0].strip().strip('"')
    return ""


def _manifest_name(manifest_text):
    return _manifest_scalar(manifest_text, "name")


def _manifest_value(manifest_text, key):
    lines = manifest_text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{key}\s*=\s*(.*)$", line.strip())
        if m:
            val = m.group(1).strip()
            # A triple-quoted value may run over several lines. Checked before
            # any quote stripping, which would remove the opening """ too and
            # leave only the first line.
            if val.startswith('"""') and (len(val) < 6 or not val.endswith('"""')):
                rest = []
                for cont in lines[i + 1:]:
                    if cont.strip().endswith('"""'):
                        rest.append(cont.strip()[:-3])
                        break
                    rest.append(cont.strip())
                return (val[3:] + "\n" + "\n".join(rest)).strip()
            return val.strip('"')
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


_github_pace_lock = threading.Lock()
_github_last_request = 0.0


def _github_pace():
    global _github_last_request
    with _github_pace_lock:
        wait = _github_last_request + GITHUB_MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _github_last_request = time.monotonic()


def _github_first_time_budget():
    """How many first-time repo checks fit above the quota reserve."""
    if not GITHUB_TOKEN:
        return GITHUB_NEW_REPOSITORY_BUDGET
    try:
        # /rate_limit is free: it does not count against the quota.
        remaining = http_get_json(
            f"{GITHUB_API}/rate_limit", timeout=30, headers=_github_headers()
        )["resources"]["core"]["remaining"]
    except Exception:  # noqa: BLE001 - unknown quota: use the proven rate
        return GITHUB_NEW_REPOSITORY_BUDGET
    # A first-time check can take a second request (fork parent, next page).
    return max(0, min(
        GITHUB_NEW_REPOSITORY_BUDGET_AUTHENTICATED,
        (remaining - GITHUB_QUOTA_RESERVE) // 2,
    ))


def _github_headers():
    _github_pace()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _github_outage(exc):
    """True for a quota answer (403/429), 408, 5xx or network fault.

    Those are the failures a last verified answer may stand in for; a 404 or
    any other refusal is real and must fail.
    """
    if isinstance(exc, HTTPError):
        return exc.code in (403, 408, 429) or 500 <= exc.code < 600
    return isinstance(exc, GITHUB_TRANSIENT_ERRORS)


def _github_quota_spent(exc):
    """True when the hourly quota is gone: retrying before reset cannot help.

    A secondary limit leaves quota remaining and is worth the short retries;
    an exhausted primary one would only add 50 seconds per request.
    """
    return exc.headers is not None and exc.headers.get("X-RateLimit-Remaining") == "0"


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
            if attempt == 3 or _github_quota_spent(exc):
                raise
        except GITHUB_TRANSIENT_ERRORS:
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
            if attempt == 3 or _github_quota_spent(exc):
                raise
        except GITHUB_TRANSIENT_ERRORS:
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
            if attempt == 3 or _github_quota_spent(exc):
                raise
            continue
        except GITHUB_TRANSIENT_ERRORS:
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
        batches = [
            pending[offset:offset + batch_size]
            for offset in range(0, len(pending), batch_size)
        ]
        while batches:
            batch = batches.pop()
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
            try:
                data = _github_graphql("query {" + "\n".join(fields) + "}")
            except RuntimeError as exc:
                # The Actions GITHUB_TOKEN gets a smaller per-query resource
                # allowance than a personal token, and a batch of large
                # accounts exceeds it (discovery failed every build for nine
                # days this way). Halve the batch and retry; one account
                # that still exceeds it raises as before.
                if "RESOURCE_LIMITS_EXCEEDED" not in str(exc) or len(batch) == 1:
                    raise
                middle = len(batch) // 2
                batches.extend([batch[:middle], batch[middle:]])
                continue
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
        if isinstance(repo, str) and "/" in repo and not repo.startswith("forgejo:"):
            # "forgejo:owner/name" markers belong to the Forgejo source,
            # which keeps its own repository list; GitHub must not probe
            # api.github.com for them.
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


def _github_asset_entry(candidate, source="github_owner",
                        repo_url_base="https://github.com"):
    """Download one author-owned bundle, validate its manifest, and normalize it.

    The Forgejo source shares this builder with its own source label and
    repository URL base; the bundle validation is identical.
    """
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

    repo_url = f"{repo_url_base}/{candidate['repo']}"
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
        "source": source,
        "sha256": digest,
    }


def _forgejo_asset_entry(candidate):
    """Download one Forgejo-hosted bundle, validate it, and normalize it."""
    return _github_asset_entry(
        candidate, source="forgejo_owner", repo_url_base=FORGEJO_WEB
    )


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
            try:
                _parent_candidates, state = future.result()
            except (HTTPError, *GITHUB_TRANSIENT_ERRORS) as exc:
                cached = parent_release_state.get(parent_key)
                verified = isinstance(cached, dict) and isinstance(
                    cached.get("candidates"), list)
                if not _github_outage(exc) or not verified:
                    raise
                if isinstance(exc, HTTPError):
                    exc.close()
                log(f"GitHub fork parent {parent}: {exc}; reusing last "
                    "verified release state")
                state = cached
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


def _github_search_repository_stamps():
    """Return {full_name: updated_at} for the repositories matching the
    search discovery query, most recently updated first."""
    stamps = {}
    query = quote(GITHUB_SEARCH_DISCOVERY_QUERY, safe="")
    for page in range(1, GITHUB_SEARCH_DISCOVERY_PAGES + 1):
        data = _github_json(
            f"{GITHUB_API}/search/repositories?q={query}"
            f"&sort=updated&order=desc&per_page=100&page={page}"
        )
        items = data.get("items") if isinstance(data, dict) else None
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            full_name = (item.get("full_name") or "").strip()
            if "/" in full_name:
                stamps[full_name] = (item.get("updated_at") or "").strip()
        if len(items) < 100:
            break
    return stamps


def _search_discovery_updates(stamps, known_folded, seen):
    """Decide which searched repositories join the pending queue.

    `known_folded` holds the casefolded names already scanned, queued or
    catalogued; those are skipped and dropped from the remembered stamps.
    Any other repository is queued only when it is new or its `updated_at`
    stamp advanced since the search last checked it, so a repository that
    was validated and rejected is left alone until it changes.
    """
    queued = set()
    new_seen = {}
    for full_name, stamp in stamps.items():
        folded = full_name.casefold()
        if folded in known_folded:
            continue
        new_seen[folded] = stamp
        if (
            seen.get(folded) != stamp
            and len(queued) < GITHUB_SEARCH_DISCOVERY_BUDGET
        ):
            queued.add(full_name)
    return queued, new_seen


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
    artifact_state = discovery.get("artifact_state")
    if not isinstance(artifact_state, dict):
        artifact_state = {}
    new_artifact_state = {}
    new_release_state = {
        repo: state
        for repo, state in release_state.items()
        if not scanned_repositories
        or repo.casefold() in scanned_folded
    }
    last_owner_scan = discovery.get("last_owner_scan") or 0
    last_search_scan = discovery.get("last_search_scan") or 0
    search_seen = discovery.get("search_seen")
    if not isinstance(search_seen, dict):
        search_seen = {}
    search_seen = {
        repo: stamp
        for repo, stamp in search_seen.items()
        if isinstance(repo, str) and isinstance(stamp, str)
    }
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
            except (RuntimeError, HTTPError) as exc:
                # GitHub reports an over-budget GraphQL query as
                # RESOURCE_LIMITS_EXCEEDED rather than RATE_LIMITED; both are
                # transient, so degrade to the cached repositories and retry
                # discovery on the next run instead of failing the build.
                # A raw HTTPError from the GraphQL endpoint (e.g. 502/503
                # after the built-in retries) is transient the same way.
                transient = (
                    "RATE_LIMITED" in str(exc)
                    or "RESOURCE_LIMITS_EXCEEDED" in str(exc)
                    or (
                        isinstance(exc, HTTPError)
                        and (exc.code in (403, 429) or 500 <= exc.code < 600)
                    )
                )
                if not transient or not addon_repositories:
                    raise
                log(
                    "GitHub owner repository discovery was rate-limited; "
                    "checking all cached add-on repositories and retrying discovery later: "
                    f"{str(exc)[:300]}"
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
        if (
            time.time() - last_search_scan
            >= GITHUB_SEARCH_DISCOVERY_TTL_SECONDS
        ):
            try:
                search_stamps = _github_search_repository_stamps()
            except Exception as exc:  # noqa: BLE001 - search is auxiliary
                # Search discovery only ever adds candidates; a failure
                # keeps the previous state, leaves the scan due so the
                # next build retries, and never blocks publication.
                log(
                    "GitHub search discovery failed; keeping the previous "
                    f"search state and retrying next run: {str(exc)[:300]}"
                )
            else:
                known_folded = (
                    scanned_folded
                    | {repo.casefold() for repo in pending_repositories}
                    | {repo.casefold() for repo in addon_repositories}
                )
                search_queued, search_seen = _search_discovery_updates(
                    search_stamps, known_folded, search_seen
                )
                if search_queued:
                    pending_repositories.update(search_queued)
                    log(
                        f"GitHub search: queued {len(search_queued)} new or "
                        "changed repositories for .nvda-addon validation"
                    )
                last_search_scan = int(time.time())
        candidates = []
    else:
        for spec in owners:
            try:
                owner_repositories, owner_fork_parents = (
                    _github_owner_repositories(spec)
                )
            except (HTTPError, *GITHUB_TRANSIENT_ERRORS) as exc:
                # The listing only confirms repositories already cached; on
                # the 60-an-hour quota, check those and list again next run.
                if not _github_outage(exc):
                    raise
                if isinstance(exc, HTTPError):
                    exc.close()
                log(f"GitHub owner {spec.get('login')}: repository list "
                    f"unavailable ({exc}); checking its cached repositories")
                continue
            repositories.update(owner_repositories)
            fork_parents.update(owner_fork_parents)
        candidates = []
    if pending_repositories:
        batch = sorted(pending_repositories, key=str.casefold)
        batch = batch[:_github_first_time_budget()]
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
    unverified = []
    vanished = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        # Artifact repositories first: they cost two requests and, until one
        # verifies, have no saved answer to stand in for a spent quota.
        futures = {
            pool.submit(_github_artifact_candidates, spec): ("artifact", spec.get("repo"))
            for spec in artifact_specs
        }
        futures.update({
            pool.submit(
                _github_release_asset_state,
                repo,
                release_state.get(repo.casefold()),
            ): ("release", repo)
            # Never-verified repositories first: on a small quota they are
            # the ones with no last verified state to fall back on.
            for repo in sorted(repositories, key=lambda repo: isinstance(
                (release_state.get(repo.casefold()) or {}).get("candidates"),
                list,
            ))
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
                    new_artifact_state[(source_name or "").casefold()] = result
            except Exception as exc:  # noqa: BLE001 - aggregate source failures
                if _github_outage(exc):
                    if isinstance(exc, HTTPError):
                        exc.close()
                    folded = (source_name or "").casefold()
                    if source_kind == "artifact":
                        stale = artifact_state.get(folded)
                        stale_candidates = stale if isinstance(stale, list) else None
                    else:
                        stale = release_state.get(folded)
                        stale_candidates = (
                            stale.get("candidates") if isinstance(stale, dict) else None
                        )
                        if not isinstance(stale_candidates, list):
                            stale_candidates = None
                    if stale_candidates is not None:
                        # Outage or spent quota: publish the last verified
                        # answer, and the next run re-checks it.
                        if source_kind == "artifact":
                            new_artifact_state[folded] = stale
                        else:
                            new_release_state[folded] = stale
                        candidates.extend(stale_candidates)
                        log(
                            "GitHub rate limit hit; reusing last verified "
                            f"{source_kind} state for {source_name}"
                        )
                        continue
                    if (
                        source_kind == "release"
                        and folded not in release_state
                        and folded not in configured_repositories
                    ):
                        # Never published, so nothing disappears by waiting.
                        pending_repositories.add(source_name)
                        continue
                    # Nothing verified to stand in. GitHub quota must never
                    # hold back the rest of the catalog: skip this repo for one
                    # build (the other catalogs still list most of its
                    # add-ons) and retry it next run.
                    unverified.append(source_name)
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
    if unverified:
        log(
            "GitHub unavailable and never verified; skipped for this build, "
            "retried next run: "
            + ", ".join(sorted(unverified, key=str.casefold))
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
        "last_search_scan": last_search_scan,
        "search_seen": search_seen,
        "configured_owners": configured_owners,
        "fork_policy": "newer-release-only-v1",
        "scanned_repositories": sorted(scanned_repositories, key=str.casefold),
        "pending_repositories": sorted(pending_repositories, key=str.casefold),
        "addon_repositories": sorted(addon_repositories, key=str.casefold),
        "release_state": new_release_state,
        "fork_parents": fork_parents,
        "parent_release_state": parent_release_state,
        "artifact_state": new_artifact_state,
    }
    if failures:
        # Discovery still saves what it verified: the next run re-checks the
        # saved release states through their ETags for free and retries only
        # the rest, so a quota burst converges across runs instead of
        # refetching everything. Old entries are preserved untouched -- this
        # is a progress save, not a publish, and the raise below still stops
        # this build from publishing an incomplete author set.
        progress = dict(old_cache)
        progress["__discovery__"] = discovery_snapshot
        _write_github_owner_cache(progress, cache_path)
        log(
            f"GitHub author discovery saved {len(new_release_state)} verified "
            f"release states and will retry {len(failures)} repositories next run"
        )
        raise RuntimeError(
            "refusing to publish incomplete GitHub author discovery: "
            + "; ".join(failures)
        )
    existing_by_url = {
        entry.get("download_url"): entry
        for entry in (existing_entries or [])
        if entry.get("download_url")
    }
    new_cache = {"__discovery__": discovery_snapshot}
    if FORGEJO_CACHE_SECTION in old_cache:
        # The Forgejo source shares this cache file; its section rides along
        # through every write here, periodic progress saves included.
        new_cache[FORGEJO_CACHE_SECTION] = old_cache[FORGEJO_CACHE_SECTION]
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


def _load_forgejo_owners(path=FORGEJO_OWNERS_PATH):
    """Load Forgejo author accounts and their add-on repositories.

    The file shares githubOwners.json's shape, but every repository must be
    listed explicitly: the Forgejo source does no account discovery.
    """
    try:
        with open(path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    owners = data.get("owners", [])
    if not isinstance(owners, list):
        return []
    return [
        spec for spec in owners
        if isinstance(spec, dict) and (spec.get("login") or "").strip()
    ]


def _forgejo_headers():
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if FORGEJO_TOKEN:
        headers["Authorization"] = f"token {FORGEJO_TOKEN}"
    return headers


def _forgejo_json(url, timeout=120):
    """Fetch Forgejo JSON with bounded retries."""
    for attempt, delay in enumerate((0, 5, 15, 30)):
        if delay:
            time.sleep(delay)
        try:
            return http_get_json(url, timeout=timeout, headers=_forgejo_headers())
        except HTTPError as exc:
            if exc.code not in (403, 429) and not 500 <= exc.code < 600:
                raise
            if attempt == 3:
                raise
        except FORGEJO_TRANSIENT_ERRORS:
            if attempt == 3:
                raise
    raise RuntimeError(f"Forgejo request did not complete: {url}")


def _forgejo_json_conditional(url, etag=None, timeout=120):
    """Fetch Forgejo JSON and preserve ETags when the instance sends them."""
    for attempt, delay in enumerate((0, 5, 15, 30)):
        if delay:
            time.sleep(delay)
        headers = _forgejo_headers()
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
        except FORGEJO_TRANSIENT_ERRORS:
            if attempt == 3:
                raise
    raise RuntimeError(f"Forgejo conditional request did not complete: {url}")


def _forgejo_owner_repositories(spec):
    """Return the configured repositories for one Forgejo owner."""
    login = (spec.get("login") or "").strip()
    if not login:
        raise ValueError("Forgejo owner entry has no login")
    excluded = {
        name.strip().casefold()
        for name in spec.get("exclude_repositories", [])
        if isinstance(name, str) and name.strip()
    }
    return sorted(
        {
            f"{login}/{name.strip()}"
            for name in spec.get("repositories", [])
            if (
                isinstance(name, str)
                and name.strip()
                and name.strip().casefold() not in excluded
            )
        },
        key=str.casefold,
    )


def _forgejo_release_asset_state(repo, previous_state=None):
    """Return current candidates and conditional-request state for one repo."""
    owner, name = repo.split("/", 1)
    url = (
        f"{FORGEJO_API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        "/releases?limit=20"
    )
    if not isinstance(previous_state, dict):
        previous_state = {}
    # Same state shape as the GitHub source, so fork comparisons and cache
    # reuse work unchanged. Forgejo sends no ETag today, so the stored one
    # stays None and each build re-lists; a handful of repositories costs
    # nothing to re-list.
    cached_etag = (
        previous_state.get("etag")
        if "latest_version" in previous_state
        else None
    )
    releases, etag, not_modified = _forgejo_json_conditional(
        url,
        etag=cached_etag,
    )
    if not_modified:
        cached_candidates = previous_state.get("candidates")
        if not isinstance(cached_candidates, list):
            raise RuntimeError(f"Forgejo returned 304 without cached candidates: {repo}")
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


def _forgejo_repository_fork_parent(repo):
    owner, name = repo.split("/", 1)
    metadata = _forgejo_json(
        f"{FORGEJO_API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    )
    if not metadata.get("fork"):
        return None
    parent = metadata.get("parent") or {}
    parent_name = parent.get("full_name")
    if not parent_name:
        raise RuntimeError(f"Forgejo fork has no originating repository: {repo}")
    return parent_name


def _forgejo_fork_release_qualifies(repo, fork_version=None):
    """Return true for originals or forks released beyond their parent version."""
    parent = _forgejo_repository_fork_parent(repo)
    if not parent:
        return True
    if fork_version is None:
        _candidates, fork_state = _forgejo_release_asset_state(repo)
        fork_version = _release_state_version(fork_state)
    _parent_candidates, parent_state = _forgejo_release_asset_state(parent)
    parent_version = _release_state_version(parent_state)
    qualifies = (
        fork_version is not None
        and parent_version is not None
        and fork_version > parent_version
    )
    if not qualifies:
        log(
            f"Forgejo fork {repo}: release {fork_version or 'unknown'} is not newer "
            f"than parent {parent} release {parent_version or 'unknown'}"
        )
    return qualifies


def fetch_forgejo_owners(
    config_path=FORGEJO_OWNERS_PATH,
    cache_path=GITHUB_OWNER_CACHE_PATH,
    existing_entries=None,
):
    """Fetch every released add-on owned by the configured Forgejo authors.

    The Forgejo source is an explicit repository list: the owners publish
    their own originals there, so there is no account discovery, no global
    search, and no fork filtering. Entries and release state live in the
    shared author cache file under FORGEJO_CACHE_SECTION, reusing the
    workflow's existing cache steps. Like the GitHub source, a failed
    repository aborts the source rather than silently publishing an
    incomplete author set.
    """
    global FORGEJO_OWNER_REJECTIONS
    FORGEJO_OWNER_REJECTIONS = []
    owners = _load_forgejo_owners(config_path)
    old_cache = _load_github_owner_cache(cache_path)
    section = old_cache.get(FORGEJO_CACHE_SECTION)
    if not isinstance(section, dict):
        section = {}
    release_state = section.get("release_state")
    if not isinstance(release_state, dict):
        release_state = {}
    old_entries = section.get("entries")
    if not isinstance(old_entries, dict):
        old_entries = {}
    repositories = set()
    for spec in owners:
        repositories.update(_forgejo_owner_repositories(spec))
    candidates = []
    new_release_state = {}
    failures = []
    for repo in sorted(repositories, key=str.casefold):
        try:
            repo_candidates, repo_state = _forgejo_release_asset_state(
                repo,
                release_state.get(repo.casefold()),
            )
        except Exception as exc:  # noqa: BLE001 - aggregate source failures
            failures.append(f"{repo}: {exc}")
            continue
        candidates.extend(repo_candidates)
        new_release_state[repo.casefold()] = repo_state
    if failures:
        raise RuntimeError(
            "refusing to publish incomplete Forgejo author discovery: "
            + "; ".join(failures)
        )
    existing_by_url = {
        entry.get("download_url"): entry
        for entry in (existing_entries or [])
        if entry.get("download_url")
    }
    new_entries = {}
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
            entry["source"] = "forgejo_owner"
            entry["source_url"] = f"{FORGEJO_WEB}/{item['repo']}"
            entries.append(entry)
            new_entries[cache_key] = entry
            reused_catalog += 1
            continue
        cached = old_entries.get(cache_key)
        if isinstance(cached, dict):
            if cached.get("_invalid"):
                FORGEJO_OWNER_REJECTIONS.append({
                    "addonId": cached.get("asset_name") or item.get("asset_name"),
                    "source": "forgejo_owner",
                    "reason": cached["_invalid"],
                })
                cached = dict(cached)
                cached["_repo"] = f"forgejo:{item.get('repo')}"
                new_entries[cache_key] = cached
                reused_cache += 1
                continue
            entry = dict(cached)
            entry["source"] = "forgejo_owner"
            entry["source_url"] = f"{FORGEJO_WEB}/{item['repo']}"
            entries.append(entry)
            new_entries[cache_key] = entry
            reused_cache += 1
            continue
        pending.append(item)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_forgejo_asset_entry, item): item for item in pending}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                entry = future.result()
                entries.append(entry)
                new_entries[_owner_cache_key(item)] = entry
            except HTTPError as exc:
                if exc.code not in (404, 410):
                    failures.append(
                        f"{item['repo']}/{item['asset_name']}: {exc}"
                    )
                    continue
                reason = f"unavailable Forgejo release asset: HTTP {exc.code}"
                FORGEJO_OWNER_REJECTIONS.append({
                    "addonId": item.get("asset_name"),
                    "source": "forgejo_owner",
                    "reason": reason,
                })
                new_entries[_owner_cache_key(item)] = {
                    "_invalid": reason,
                    "asset_name": item.get("asset_name"),
                    "_repo": f"forgejo:{item.get('repo')}",
                }
            except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, ValueError, RuntimeError) as exc:
                reason = f"invalid NVDA add-on bundle: {exc}"
                FORGEJO_OWNER_REJECTIONS.append({
                    "addonId": item.get("asset_name"),
                    "source": "forgejo_owner",
                    "reason": reason,
                })
                new_entries[_owner_cache_key(item)] = {
                    "_invalid": reason,
                    "asset_name": item.get("asset_name"),
                    "_repo": f"forgejo:{item.get('repo')}",
                }
            except Exception as exc:  # noqa: BLE001 - aggregate bundle failures
                failures.append(
                    f"{item['repo']}/{item['asset_name']}: {exc}"
                )
    new_cache = {
        key: value for key, value in old_cache.items()
        if key != FORGEJO_CACHE_SECTION
    }
    new_cache[FORGEJO_CACHE_SECTION] = {
        "release_state": new_release_state,
        "entries": new_entries,
    }
    _write_github_owner_cache(new_cache, cache_path)
    if failures:
        raise RuntimeError(
            "refusing to publish incomplete Forgejo author add-ons: "
            + "; ".join(failures)
        )
    log(
        f"Forgejo authors: reused {reused_catalog} catalog records and "
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
                # The asset's size per GitHub: a change means the author
                # replaced the file under the same URL (see cached_download).
                "catalog_size": a.get("download_size_bytes") or None,
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

#: The text each add-on had BEFORE the generated overlay replaced it, written
#: for auto_translate.py. That script reads the built addons.json, where the
#: overlay is already applied, so without this it saw the English, found
#: nothing to translate, and dropped the add-on from the next overlay -- and
#: the build after that published the original again. Every build flipped
#: about 200 names between English and the original.
AUTO_TRANSLATION_SOURCES_PATH = "autoTranslationSources.json"
#: addonId -> fields whose English comes only from the generated overlay.
AUTO_ONLY_FIELDS = {}
#: addonId -> {"displayName"/"description": pre-overlay text}, filled by
#: transform() and written by main().
AUTO_TRANSLATION_SOURCES = {}


def _read_overlay(candidate):
    """Read a translations overlay file as {addonId: fields}, or {}."""
    try:
        with open(candidate, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict):
        inner = data.get("translations", data)
        return inner if isinstance(inner, dict) else {}
    return {}


def auto_only_fields(path=TRANSLATIONS_PATH, auto_path=AUTO_TRANSLATIONS_PATH):
    """Return {addonId: {field}} for fields only the generated overlay sets.

    A field the hand-maintained file also sets is left out: the human's English
    is published whatever the model says, so its original is never worth
    sending to the model.
    """
    human = _read_overlay(path)
    result = {}
    for addon_id, fields in _read_overlay(auto_path).items():
        if not isinstance(fields, dict):
            continue
        own = human.get(addon_id)
        own = own if isinstance(own, dict) else {}
        auto = {field for field in ("summary", "description")
                if fields.get(field) and not own.get(field)}
        if auto:
            result[addon_id] = auto
    return result


def load_translations(path=TRANSLATIONS_PATH, auto_path=AUTO_TRANSLATIONS_PATH):
    """Load the English overlay: hand-maintained first, generated beneath.

    Merged per field rather than per add-on, so a human who corrected only a
    summary still gets the generated description, and never loses their summary
    to the model on the next run.
    """
    read = _read_overlay
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
    """Overlay configured English metadata onto a catalog entry.

    Returns {field: original text} for the fields the generated overlay
    replaced, so transform() can record what auto_translate.py should see.
    """
    key = entry["name"]
    if key not in TRANSLATIONS:
        key = entry.get("catalog_name")
    tr = TRANSLATIONS.get(key)
    replaced = {
        field: entry.get(field) or ""
        for field in AUTO_ONLY_FIELDS.get(key, ())
        if tr and tr.get(field)
    }
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
    return replaced


def _display_name(entry, name):
    """The published displayName for an entry's current summary."""
    # Authors sometimes put a description in manifest.ini's summary,
    # which is the field NVDA shows as the add-on's name. Publishing that
    # verbatim leaves a paragraph where a title belongs, and the real name
    # nowhere at all -- unusable when arrowing a list of names.
    # An add-on whose own name is not English gets the English name first
    # and its real name after "AKA", so it is both understandable and
    # still recognisable from its own documentation.
    return compose_display_name(
        best_display_name(
            clean_text(entry.get("summary")),
            clean_text(entry.get("manifest_summary")),
            name,
        ) or name,
        clean_text(entry.get("manifest_summary")),
    )


def _record_auto_source(entry, name, replaced):
    """Remember the pre-overlay text of fields the generated overlay replaced."""
    if not replaced:
        return
    original = {}
    if "summary" in replaced:
        original["displayName"] = _display_name(
            dict(entry, summary=replaced["summary"]), name)
    if "description" in replaced:
        original["description"] = replaced["description"]
    AUTO_TRANSLATION_SOURCES.setdefault(name, original)


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

    _record_auto_source(entry, name, _translate_entry(entry))

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
        "displayName": _display_name(entry, name),
        "description": entry.get("description") or "",
        "publisher": author,
        "channel": entry.get("channel") or "stable",
        # NVDA saves the download as "{addonId}-{addonVersionName}.nvda-addon".
        "addonVersionName": _ILLEGAL_ID_CHARS_RE.sub("-", version or ""),
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


VT_API = "https://www.virustotal.com/api/v3"
VT_CACHE_PATH = os.path.join(PINNED_BUNDLE_CACHE_PATH, "virusTotal.json")
#: Free public API: 4 requests a minute, 500 a day. A build every half hour
#: spending at most 10 stays under both with room for manual runs.
VT_CALLS_PER_BUILD = int(os.environ.get("MIRROR_VT_CALLS_PER_BUILD") or 10)
VT_DAILY_BUDGET = 450
VT_CALL_SPACING_SECONDS = 15
VT_REFRESH_SECONDS = 7 * 24 * 60 * 60
#: After a URL submission VirusTotal fetches and scans the file itself.
VT_PENDING_RECHECK_SECONDS = 30 * 60
VT_RESUBMIT_SECONDS = 7 * 24 * 60 * 60
#: One engine alone flags Python bundles often enough to be noise.
VT_MALICIOUS_BLOCK = 3
#: Scanned after the rest: the store scans its own, a pin is bytes this
#: mirror repackaged, and Forgejo releases are the owner's own.
VT_EXEMPT_SOURCES = ("official", "pinned", "forgejo_owner")


def _vt_request(path, key, data=None):
    """One VirusTotal API call: (status, json or None). 404 is an answer."""
    request = Request(
        f"{VT_API}/{path}",
        data=urlencode(data).encode() if data else None,
        headers={"x-apikey": key, "User-Agent": USER_AGENT},
    )
    try:
        with urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        exc.close()
        return exc.code, None


def _vt_engine_count(stats):
    """How many engine answers a VirusTotal stats dict holds (0 = still queued)."""
    if not isinstance(stats, dict):
        return 0
    return sum(v for v in stats.values() if isinstance(v, int))


def _vt_scan_result(sha256, record):
    stats = record.get("stats") if isinstance(record, dict) else None
    # A file VirusTotal has fetched but not yet scanned answers all zeros;
    # publishing that would show a scan that never happened.
    if not _vt_engine_count(stats):
        return None
    return {
        "virusTotal": [{"last_analysis_stats": stats}],
        "vtScanUrl": f"https://www.virustotal.com/gui/file/{sha256}",
    }


def virustotal_scans(entries, now=None, cache_path=None, key=None, priority=()):
    """Attach VirusTotal results to every published file the store lacks them for.

    The official store scans what it publishes; nothing else does. A file is
    looked up by its SHA-256 (no bytes leave here). One VirusTotal has never
    seen is submitted by its download URL, so VirusTotal fetches it once
    itself -- never a re-download by this mirror -- and a later build reads
    the verdict. Answers are cached per SHA-256 and refreshed weekly, inside
    the free API's per-minute and daily limits. Without VT_API_KEY nothing is
    asked and cached answers still apply.

    ``priority`` holds SHA-256s waiting on a verdict before they may publish;
    they are asked about before any backfill. Returns the entries
    VT_MALICIOUS_BLOCK or more engines call malicious.
    """
    key = key if key is not None else os.environ.get("VT_API_KEY", "").strip()
    now = time.time() if now is None else now
    cache_path = cache_path or VT_CACHE_PATH
    cache = load_json_cache(cache_path)
    budget = cache.get("__budget__") if isinstance(cache.get("__budget__"), dict) else {}
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if budget.get("day") != today:
        budget = {"day": today, "used": 0}
    scannable = [
        e for e in entries
        if not e.get("scan_results") and (e.get("sha256") or "").strip()
    ]

    def due(e):
        record = cache.get(e["sha256"].lower())
        if not isinstance(record, dict):
            return True
        if record.get("stats"):
            return now - record.get("checked", 0) >= VT_REFRESH_SECONDS
        return now - record.get("checked", 0) >= VT_PENDING_RECHECK_SECONDS

    calls = 0
    if key:
        # Files held back for a verdict first, then never asked, then newest.
        queue = sorted(
            (e for e in scannable if due(e)),
            key=lambda e: (e["sha256"].lower() not in priority,
                           e["sha256"].lower() in cache,
                           -(e.get("submission_ms") or 0)),
        )
        for e in queue:
            if calls >= VT_CALLS_PER_BUILD or budget["used"] >= VT_DAILY_BUDGET:
                break
            sha = e["sha256"].lower()
            record = dict(cache.get(sha) or {})
            if calls:
                time.sleep(VT_CALL_SPACING_SECONDS)
            status, body = _vt_request(f"files/{sha}", key)
            calls += 1
            budget["used"] += 1
            if status == 429:
                log("VirusTotal quota reached; the rest wait for a later build")
                break
            if status == 200:
                attributes = (body or {}).get("data", {}).get("attributes", {})
                stats = attributes.get("last_analysis_stats")
                record["checked"] = now
                if _vt_engine_count(stats):
                    record["stats"] = stats
                else:
                    # Known but still queued: ask again after the short wait.
                    record.pop("stats", None)
            elif status == 404:
                record["checked"] = now
                url = e.get("download_url") or ""
                if (url.startswith("https://")
                        and ("submitted" not in record
                             or now - record["submitted"] >= VT_RESUBMIT_SECONDS)
                        and calls < VT_CALLS_PER_BUILD
                        and budget["used"] < VT_DAILY_BUDGET):
                    time.sleep(VT_CALL_SPACING_SECONDS)
                    status, _body = _vt_request("urls", key, {"url": url})
                    calls += 1
                    budget["used"] += 1
                    if status == 200:
                        record["submitted"] = now
            else:
                log(f"VirusTotal answered HTTP {status} for {e.get('name')}; retried later")
                continue
            cache[sha] = record
        if calls:
            cache["__budget__"] = budget
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(cache_path + ".tmp", cache_path)

    blocked = []
    scanned = 0
    for e in scannable:
        scan = _vt_scan_result(e["sha256"].lower(), cache.get(e["sha256"].lower()))
        if not scan:
            continue
        scanned += 1
        e["scan_results"] = scan
        if (scan["virusTotal"][0]["last_analysis_stats"].get("malicious") or 0) >= VT_MALICIOUS_BLOCK:
            blocked.append(e)
    log(f"VirusTotal: {scanned}/{len(scannable)} non-store files scanned; "
        f"{calls} API calls this build, {budget.get('used', 0)} today")
    return blocked


def virustotal_gate(results, previous):
    """Scan (entry, digest, size) results; drop what VirusTotal flags.

    Returns (kept results, rejection rows). Every file publishes at once (the
    owner's call, 2026-11-10); a file VT_MALICIOUS_BLOCK or more engines call
    malicious is removed in the first build that has the verdict. Files the
    last build did not publish are asked about before the backfill.
    """
    for e, digest, _size in results:
        e.setdefault("sha256", digest)
    published_shas = {(o.get("sha256") or "").lower() for o in previous}
    new_files = {
        e["sha256"].lower() for e, _d, _s in results
        if e.get("source") not in VT_EXEMPT_SOURCES
        and e["sha256"].lower() not in published_shas
    }
    flagged = {id(e) for e in virustotal_scans(
        [e for e, _d, _s in results], priority=new_files)}
    kept, held = [], []
    for item in results:
        e = item[0]
        if id(e) in flagged:
            stats = e["scan_results"]["virusTotal"][0]["last_analysis_stats"]
            held.append({
                "addonId": e.get("name"), "source": e.get("source"),
                "reason": (f"VirusTotal: {stats.get('malicious')} engines "
                           "flag it as malicious"),
            })
        else:
            kept.append(item)
    return kept, held


def dedupe(entries):
    """Dedupe by (addonId, channel).

    The official store lists one entry PER CHANNEL for the same add-on (e.g.
    stable 2026.05.03 + dev 2024.09.09 of robEnhancements); NVDA's client
    indexes by [channel][addonId], so all variants must survive. Community
    catalogs list one entry per add-on, which lands in whatever channel the
    catalog declares.

    Preference order within the same (addonId, channel): an entry with a
    download URL beats one without; an explicitly pinned release beats
    everything; then the newest parseable version wins, from any source. Only
    a tie falls to source: Forgejo author releases > GitHub author releases >
    official (NV Access-reviewed, VirusTotal data, upstream hash) >
    nvda-addons.ru (curated, direct links) > bestmidi > the shared Spanish
    catalog.

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
        # The owner moved to Forgejo: a GitHub copy of one of their add-ons
        # never wins, whatever version it claims.
        if any(e["source"] == "forgejo_owner" for e in group):
            group = [e for e in group if e["source"] != "github_owner"]
        winner = max(
            group,
            key=lambda e: (
                1 if e["download_url"] else 0,
                # A pin is a deliberate choice of file; nothing outranks it.
                1 if e["source"] == "pinned" else 0,
                # Newest release next (2026-11-10): an author's GitHub release
                # beat a stale store copy 76 times. Every non-store file now
                # carries its own VirusTotal result (virustotal_scans).
                sanitize_version(e.get("version")) or (0, 0, 0),
                priority.get(e["source"], 0),
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

    A pre-release that NVDA ranks above stable but that was published before
    the stable release is dropped too. ColumnsReview's 2022 "20221104-dev"
    build reads as 20221104.0.0, above the 2026 stable 5.7.0, so NVDA offered
    the four-year-old build as an update. bestmidi's time is the repo's last
    push, not a release date, so it never counts as the stable release date.

    Returns (kept, rejected).
    """
    stable_versions = {}
    stable_released = {}
    for entry in entries:
        if (entry.get("channel") or "stable") != "stable":
            continue
        version = sanitize_version(entry.get("version"))
        if version is not None:
            key = entry["name"].casefold()
            stable_versions.setdefault(key, set()).add(version)
            if entry.get("submission_ms") and entry.get("source") != "bestmidi":
                stable_released[key] = max(
                    stable_released.get(key, (version, 0)),
                    (version, entry["submission_ms"]),
                )

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
        stable = stable_released.get(entry["name"].casefold())
        if (
            channel != "stable"
            and stable is not None
            and version is not None
            and version > stable[0]
            and entry.get("submission_ms")
            and entry["submission_ms"] < stable[1]
        ):
            rejected.append({
                "addonId": entry.get("name"),
                "source": entry.get("source"),
                "reason": (
                    f"{channel} build predates the stable release but would "
                    "be offered as an update to it"
                ),
            })
            continue
        kept.append(entry)
    return kept, rejected


def load_json_cache(path):
    """Load a persisted dict cache, or {} for anything unreadable.

    Used for the caches that ride along with the site (locale ETags and the
    like). A corrupt or missing file costs a rebuild of that cache,
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

    Retention keeps every API version from the newest ``years_in_full`` NVDA
    years, and only the newest API version of each year before that. See
    API_VERSION_YEARS_IN_FULL.

    ``years_in_full=0`` disables pruning.
    """
    released = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ver = _api_version_from_entry(entry)
        if ver is not None and ver >= ADDON_STORE_FIRST_API_VERSION:
            released.add(ver)

    kept = released
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
    if dropped:
        log(f"API versions not published (anyone still on one of these gets an "
            f"Add-on Store error and no compatible add-ons, raise "
            f"--api-version-years to keep them): {', '.join(dropped)}")


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


#: What an author leaves in the summary when they never named the add-on:
#: "NVDA add-on", "Addon for NVDA", the template's "Add-on user visible name".
_PLACEHOLDER_NAME_RE = re.compile(
    r"^(?:(?:an?\s+)?(?:nvda[\s-]*)?add-?ons?"
    r"(?:\s+for\s+(?:the\s+)?nvda(?:\s+screen\s+reader)?)?"
    r"|add-?on\s+user\s+visible\s+name)\.?$", re.I)


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
    # "NVDA add-on", "Addon for NVDA": a template placeholder names nothing.
    if _PLACEHOLDER_NAME_RE.match(text):
        return True
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
    # Some catalogs use the add-on's own title as its id ("Accessible n8n
    # Workflow Manager"). That is already the name; re-casing it only breaks
    # spellings like "PowerPoint" and "n8n".
    if re.search(r"\s", (addon_id or "").strip()):
        return addon_id.strip()
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


def localize_catalog(output, lang, official, authored):
    """Return the catalog as one language sees it.

    Two sources, best first:

    1. the official store's own per-language view (the author's words, already
       reviewed by NV Access),
    2. locale/<lang>/manifest.ini inside the add-on bundle (the author's words
       for everything the official store does not carry).

    Anything with no translation at either tier keeps its English string, which
    is what NVDA shows today, so a missing translation is never worse than now.
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
                    or ""
                )
                if value:
                    break
            if value and value != english:
                replacement[field] = value
        localized.append({**addon, **replacement} if replacement else addon)
    return localized


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


def _build_index_html(stats, rejected, site_base_url):
    """Home page: helper download, mirror setup steps, catalog links."""
    accepted = stats.get("accepted", 0)
    generated = stats.get("generated_at", "")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>NVDA Add-on Store Mirror</title></head><body>"
        "<h1>NVDA Add-on Store Mirror</h1>"
        f"<p>{accepted} add-ons mirrored from the "
        "<a href='https://github.com/nvaccess/addon-datastore'>official NV Access "
        "add-on store</a> (with the "
        "<a href='https://github.com/nvdacn/NVDAUpdateMirror'>Chinese community "
        "mirror</a> as failover), "
        "<a href='https://bestmidi.com/addons/'>bestmidi.com/addons/</a>, "
        "<a href='https://nvda-addons.ru/'>nvda-addons.ru</a>, "
        "<a href='https://nvda.es/'>nvda.es</a> (with "
        "<a href='https://nvda-addons.org/'>nvda-addons.org</a> failover), and "
        "validated direct author releases from GitHub and Forgejo.</p>"
        f"<p>Catalog built {generated}.</p>"
        "<h2>Download the helper add-on</h2>"
        f"<p><a href='{HELPER_DOWNLOAD_URL}'>Download addonStoreMirror "
        f"{HELPER_VERSION}</a> "
        f"(or pick a version from the <a href='{HELPER_RELEASES_URL}'>releases "
        "page</a>). It points NVDA's Add-on Store at this mirror on startup "
        "and restores the official store when disabled. Requires NVDA 2025.1 "
        "or newer.</p>"
        "<h2>Point NVDA at the mirror</h2>"
        "<ol>"
        "<li><strong>Install the helper add-on</strong> (recommended) as above.</li>"
        "<li><strong>Edit nvda.ini</strong> manually, then restart NVDA:"
        "<pre>[addonStore]\n"
        f"baseServerURL = {site_base_url}</pre></li>"
        "<li><strong>Use NVDA's built-in mirror setting</strong>: NVDA menu, "
        "Preferences, Settings, Add-on Store, Mirror server, Change..., paste "
        f"<code>{site_base_url}</code>, then OK. Available since NVDA 2025.1.</li>"
        "</ol>"
        "<h2>Browse the catalog</h2>"
        "<p><a href='store.html'>Browse every mirrored add-on</a> with search "
        "and download links, without NVDA.</p>"
        "<h2>Data</h2>"
        "<ul>"
        "<li><a href='addons.json'>addons.json</a>: the full English catalog.</li>"
        "<li><a href='stats.json'>stats.json</a>: build counts and API versions.</li>"
        "<li><a href='cacheHash.json'>cacheHash.json</a>: the global catalog hash.</li>"
        f"<li><a href='rejected.html'>{len(rejected)} rejected candidates</a>, "
        "grouped by reason.</li>"
        "</ul>"
        "<p>NVDA itself fetches {lang}/all/{apiVersion}.json for the running "
        "NVDA's own language and API version.</p>"
        "<p>Community add-ons are untested; install at your own risk. Official "
        "store add-ons include VirusTotal scan results.</p>"
        "</body></html>"
    )


def _build_store_html(stats):
    """Browsable catalog page; rows load from addons.json in the browser."""
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Browse the NVDA Add-on Store Mirror</title>
</head>
<body>
<h1>NVDA Add-on Store Mirror catalog</h1>
<p>__ACCEPTED__ add-ons, catalog built __GENERATED_AT__.
<a href="index.html">Back to the mirror home page</a></p>
<p>
<label for="search">Search name, publisher, description or add-on ID:</label>
<input type="search" id="search" autocomplete="off">
<span id="count" role="status" aria-live="polite"></span>
</p>
<noscript><p>Searching needs JavaScript. The raw catalog is
<a href="addons.json">addons.json</a>.</p></noscript>
<table>
<thead><tr>
<th scope="col">Name</th><th scope="col">Version</th>
<th scope="col">Publisher</th><th scope="col">Channel</th>
<th scope="col">NVDA</th><th scope="col">Source</th>
<th scope="col">Download</th>
</tr></thead>
<tbody id="rows"></tbody>
</table>
<script>
(function () {
  var input = document.getElementById("search");
  var count = document.getElementById("count");
  var body = document.getElementById("rows");
  function versionText(value) {
    if (!value) return "";
    return value.major + "." + value.minor + "." + value.patch;
  }
  function makeRow(row) {
    var tr = document.createElement("tr");
    var name = document.createElement("td");
    var strong = document.createElement("strong");
    strong.textContent = row.displayName || row.addonId;
    name.appendChild(strong);
    if (row.description) {
      name.appendChild(document.createElement("br"));
      var small = document.createElement("small");
      small.textContent = row.description;
      name.appendChild(small);
    }
    tr.appendChild(name);
    [["addonVersionName"], ["publisher"], ["channel"]].forEach(function (key) {
      var cell = document.createElement("td");
      cell.textContent = row[key[0]] || "";
      tr.appendChild(cell);
    });
    var range = document.createElement("td");
    range.textContent = versionText(row.minNVDAVersion) + " - " + versionText(row.lastTestedVersion);
    tr.appendChild(range);
    var source = document.createElement("td");
    source.textContent = row.storeSource || "";
    tr.appendChild(source);
    var download = document.createElement("td");
    if (row.URL && /^https?:\\/\\//.test(row.URL)) {
      var link = document.createElement("a");
      link.href = row.URL;
      link.textContent = "Download";
      download.appendChild(link);
    }
    tr.appendChild(download);
    tr._haystack = [row.displayName, row.addonId, row.publisher,
      row.description, row.storeSource].join(" ").toLowerCase();
    return tr;
  }
  function update() {
    var q = input.value.trim().toLowerCase();
    var rows = body.children;
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var match = !q || rows[i]._haystack.indexOf(q) !== -1;
      rows[i].style.display = match ? "" : "none";
      if (match) shown++;
    }
    count.textContent = q ? shown + " matching of " + rows.length : rows.length + " add-ons";
  }
  input.addEventListener("input", update);
  fetch("addons.json").then(function (response) {
    if (!response.ok) throw new Error(response.status);
    return response.json();
  }).then(function (rows) {
    rows.forEach(function (row) { body.appendChild(makeRow(row)); });
    update();
  }).catch(function () {
    count.textContent = "Could not load addons.json.";
  });
})();
</script>
</body>
</html>
"""
    return template.replace("__ACCEPTED__", str(stats.get("accepted", 0))).replace(
        "__GENERATED_AT__", str(stats.get("generated_at", ""))
    )


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
    site_base_url=None,
):
    """Write the site.

    ``localize(lang)`` returns the catalog as that language sees it and
    ``dump`` serialises a catalog the way NVDA expects; ``canonical_bytes``
    and ``compatible_bytes`` are the English renderings, kept for the
    top-level addons.json and the recorded counts. ``site_base_url`` is the
    public base URL printed on the home page's setup steps.
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

    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(_build_index_html(
            stats, rejected, site_base_url or DEFAULT_SITE_BASE_URL
        ))

    with open(os.path.join(out_dir, "store.html"), "w", encoding="utf-8") as f:
        f.write(_build_store_html(stats))

    with open(os.path.join(out_dir, "rejected.html"), "w", encoding="utf-8") as f:
        f.write(build_rejected_page(rejected, stats))

    def write_bytes(rel_path, data):
        # Static hosts and deploy tools handle symlinks inconsistently (some
        # refuse them, some dereference them and balloon size), so always
        # write real copies rather than symlinks.
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
_AUDIT_TEXT_KEYS = (
    "addonId", "displayName", "description", "publisher", "addonVersionName",
    "license", "sourceURL",
)
_AUDIT_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def _audit_row_contract(addon):
    """What NVDA needs from one row to load it and to download its file.

    NVDA builds the whole store from one loop over the rows, so one that raises
    takes every add-on with it. Applies to latest.json as much as to the
    compatible views: "include incompatible add-ons" reads it.
    """
    problems = [
        f"{key} must be text, got {addon[key]!r}"
        for key in _AUDIT_TEXT_KEYS
        if key in addon and not isinstance(addon[key], str)
    ]
    sha256 = addon.get("sha256")
    if "sha256" in addon and not (
        isinstance(sha256, str) and _AUDIT_SHA256_RE.fullmatch(sha256)
    ):
        problems.append(f"sha256 is not a digest: {sha256!r}")
    url = addon.get("URL")
    if "URL" in addon and not (
        isinstance(url, str) and url.startswith(("http://", "https://"))
    ):
        problems.append(f"URL is not downloadable: {url!r}")
    if "scanResults" in addon and not isinstance(addon["scanResults"], dict):
        problems.append(f"scanResults must be omitted or a dict: {addon['scanResults']!r}")
    file_name = f"{addon.get('addonId')}-{addon.get('addonVersionName')}.nvda-addon"
    if _ILLEGAL_ID_CHARS_RE.search(file_name):
        problems.append(f"illegal Windows file name: {file_name!r}")
    return problems


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
    unfiltered "show all" view: it is exempt from the version floors, and held
    to everything else.

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
        is_latest = ver_name == "latest"
        ver_tuple = floor = None
        if not is_latest:
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
        if is_latest and not entries:
            problems.append(f"{path}: empty, which is an empty Add-on Store")
        seen = set()
        for index, addon in enumerate(entries):
            label = f"{path}[{index}]:"
            for key in _AUDIT_REQUIRED_KEYS:
                if key not in addon:
                    problems.append(f"{label} missing required key {key!r}")
            channel = addon.get("channel")
            if channel not in _AUDIT_CHANNELS:
                problems.append(f"{label} unexpected channel {channel!r}")
            problems.extend(f"{label} {p}" for p in _audit_row_contract(addon))
            for key in ("minNVDAVersion", "lastTestedVersion", "addonVersionNumber"):
                version_dict = addon.get(key)
                try:
                    if set(version_dict) != {"major", "minor", "patch"}:
                        raise ValueError(version_dict)
                    parsed = (
                        int(version_dict["major"]),
                        int(version_dict["minor"]),
                        int(version_dict["patch"]),
                    )
                except (KeyError, TypeError, ValueError):
                    problems.append(f"{label} {key} is not a major/minor/patch dict")
                    continue
                if is_latest:
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

def _pinned_repo_prefixes(todo):
    """Download-URL prefixes of pinned repos, for catalog-row suppression.

    A pinned fork is renamed so it can sit beside the original sharing its
    manifest name. Any non-pinned row whose download URL sits under the
    fork's repo still carries that shared name, so the build drops it.
    Pins live on GitHub or the owner's Forgejo instance; both count.
    """
    return tuple(
        e["source_url"].rstrip("/").casefold() + "/"
        for e in todo
        if e.get("source") == "pinned"
        and (e.get("source_url") or "").casefold().startswith(
            ("https://github.com/", FORGEJO_WEB.casefold() + "/")
        )
    )


def main():
    parser = argparse.ArgumentParser(description="Build a NVDA add-on store mirror.")
    parser.add_argument("--out", default="public")
    parser.add_argument("--sources", default=",".join(ALL_SOURCES),
                        help=("comma-separated sources: official,bestmidi,ru,es,"
                              "github_owner,forgejo_owner,pinned"))
    parser.add_argument("--locales", help="comma-separated locale override")
    parser.add_argument("--api-versions", help="comma-separated apiVersion override")
    parser.add_argument(
        "--api-version-years", type=int, default=API_VERSION_YEARS_IN_FULL,
        help=("publish every API version from this many recent NVDA years, "
              "plus the newest one of each older year; 0 keeps them all. "
              "Each one costs roughly 120 MB across all locales."),
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
                        default=DEFAULT_SITE_BASE_URL,
                        help="public base URL of the mirror (used for hosted pinned add-ons)")
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()

    sources = args.sources.split(",")
    locales = args.locales.split(",") if args.locales else LOCALES
    channels = args.channels.split(",") if args.channels else CHANNELS

    global TRANSLATIONS, AUTO_ONLY_FIELDS
    TRANSLATIONS = load_translations()
    AUTO_ONLY_FIELDS = auto_only_fields()
    AUTO_TRANSLATION_SOURCES.clear()

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
    # and the Forgejo source one small release listing per repository (the
    # instance sends no ETags, but the configured list is a handful of
    # repositories), so those do refresh on every build. This keeps the
    # hourly mirror from repeatedly pulling unchanged catalog bodies from
    # other people's servers.
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

    if "forgejo_owner" in sources:
        log("Fetching configured Forgejo authors")
        entries = fetch_forgejo_owners(existing_entries=all_entries)
        log(f"Forgejo authors: {len(entries)} add-on/channel candidates")
        all_entries.extend(entries)
        rejected.extend(FORGEJO_OWNER_REJECTIONS)

    if "pinned" in sources:
        log("Fetching pinned variant add-ons")
        entries = fetch_pinned()
        log(f"pinned: {len(entries)} add-ons")
        all_entries.extend(entries)

    # Every later stage -- translations, blocklist, dedupe, the published
    # catalog -- keys on the entry name, and NVDA turns that name into a
    # download file name, so sanitize once here (see sanitize_addon_id).
    for e in all_entries:
        e["name"] = sanitize_addon_id(e.get("name"))

    # Then correct the name itself against the bundle's manifest, wherever
    # a previous build's download already revealed it: the catalog name is
    # only a claim, the manifest name is the add-on's identity in NVDA.
    if not (args.skip_download or args.limit):
        read = read_unread_manifests(all_entries, hashcache, args.workers)
        if read:
            log(f"Read {read} unread bundle manifests by range")
    renamed = apply_manifest_names(all_entries, hashcache)
    if renamed:
        log(f"Corrected {renamed} addonIds to their bundle manifest names")
    corrected = apply_manifest_claims(all_entries, hashcache)
    if corrected:
        log(f"Corrected {corrected} version fields to their bundle manifests")

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

    # 2b. Drop every unpinned entry superseded by a pinned variant. These
    # share the generic manifest name of a pinned add-on (e.g. the four
    # "Eloquence" variants all publish name = Eloquence), so they would appear
    # as duplicates alongside the distinctly-named pinned entries. Author
    # releases count too: fastfinge's old v8 bundle kept the generic id, and
    # NVDA offered it as a "migration" to anyone who sideloaded Eloquence64RS,
    # whose "v19.1.4-RS" version NVDA cannot parse.
    excluded_names = {
        (spec.get("name") or "").strip()
        for spec in _load_excluded(PINNED_CONFIG_PATH)
    }
    # A pinned fork is renamed so it can sit beside the original that shares
    # its manifest name. Any catalog row for the fork's repo still carries
    # that shared name, and would take it from the original in dedupe.
    pinned_repos = _pinned_repo_prefixes(todo)
    if excluded_names or pinned_repos:
        kept = []
        excluded_count = 0
        for e in todo:
            if e.get("source") != "pinned" and (
                    e.get("name") in excluded_names
                    or (e.get("download_url") or "").casefold().startswith(pinned_repos)):
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

    # 2c. Drop anything on the safety blocklist (blocked.json), from every
    # source. These are add-ons the mirror must never serve at all, e.g.
    # troll bundles that present themselves as malware.
    todo, blocked_rejected = drop_blocked_entries(
        todo, _load_blocked(BLOCKED_CONFIG_PATH)
    )
    if blocked_rejected:
        rejected.extend(blocked_rejected)
        log(f"Blocked {len(blocked_rejected)} entries on the safety blocklist")

    todo, dead_rejected = drop_dead_downloads(todo)
    rejected.extend(dead_rejected)

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
            claimed = e.get("catalog_size")
            if claimed and claimed != cached.get("size"):
                cached["size_claim_checked"] = claimed
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

    # 4b. A bundle first fetched in this build only now shows its manifest,
    # after step 1 corrected everything already cached. Publishing the
    # catalog's ID or compatibility claim for one build gives NVDA a row it
    # refuses as "Add-on not supported" (Reloq-C 1.3.0, 2026-11-10), so apply
    # the manifest now. Free: the bytes were streamed for the hash anyway.
    if not args.skip_download:
        before = {id(e): e["name"] for e, _digest, _size in results}
        fresh = [e for e, _digest, _size in results]
        if apply_manifest_names(fresh, new_hashcache):
            # Dedupe already ran: a renamed row may now share an ID and
            # channel with a row that won its own group. That row keeps it,
            # and the next build dedupes the two properly.
            taken = {
                (e["name"].casefold(), e.get("channel") or "stable")
                for e in fresh if e["name"] == before[id(e)]
            }
            kept = []
            for item in results:
                e = item[0]
                key = (e["name"].casefold(), e.get("channel") or "stable")
                if e["name"] != before[id(e)] and key in taken:
                    rejected.append({
                        "addonId": before[id(e)],
                        "source": e.get("source"),
                        "reason": (f"bundle manifest names it {e['name']!r}, "
                                   "already published; dedupes next build"),
                    })
                    continue
                taken.add(key)
                kept.append(item)
            results = kept
        apply_manifest_claims(fresh, new_hashcache)

    # 4c. VirusTotal for every file the official store did not scan. Files
    # publish at once and are removed when a verdict flags them; files the
    # last build did not publish are asked about first.
    if not args.skip_download:
        try:
            with open(os.path.join(args.out, "addons.json"), encoding="utf-8") as f:
                previous = json.load(f)
        except (OSError, ValueError):
            previous = []
        results, held = virustotal_gate(
            results, previous if isinstance(previous, list) else [])
        rejected.extend(held)

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
    # of the bundle. Anything else stays in English. (There is no
    # machine-translation tier: the only translation job is non-English ->
    # English, maintained by hand in translations.json.)
    locale_cache = load_json_cache(LOCALE_CACHE_PATH)
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

    def localize(lang):
        return localize_catalog(
            output, lang, official_translations, authored_translations,
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
        site_base_url=args.site_base_url,
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
    # instead of 1.7 MB.
    for cache_path, cache_data in (
        (LOCALE_CACHE_PATH, locale_cache),
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
    # Read by auto_translate.py in the same job. Neither published nor cached:
    # every build regenerates it from the catalog it has just built.
    with open(AUTO_TRANSLATION_SOURCES_PATH, "w", encoding="utf-8") as f:
        json.dump(AUTO_TRANSLATION_SOURCES, f, ensure_ascii=False,
                  indent=1, sort_keys=True)
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
