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
import fnmatch
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
from urllib.parse import quote, urlsplit, urlunsplit

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
#: build time when reachable.
FALLBACK_BACK_COMPAT_TO = (2026, 1, 0)

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
# The Add-on Store client only shipped in NVDA 2024.1, so older API versions
# cannot consume this mirror. All 2024.1+ versions remain published permanently
# as new versions are appended.
ADDON_STORE_FIRST_API_VERSION = (2024, 1, 0)

# API version regex mirrors NVDA source/addonAPIVersion.py: year.major(.minor)
_API_VERSION_RE = re.compile(r"^(0|\d{4})\.(\d)(?:\.(\d))?$")

_INT_RUN_RE = re.compile(r"\d+")

#: Cyrillic block, used to detect Russian (nvda-addons.ru) text so the store
#: can prefer English where an English sibling source exists.
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_NON_LATIN_SCRIPT_RE = re.compile(
    r"[\u0370-\u06ff\u0900-\u0e7f\u10a0-\u10ff"
    r"\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]"
)
_NON_ENGLISH_LATIN_CHANGELOG_RE = re.compile(
    r"(?i)\b(?:versi[oó]n|a[ñn]adid[ao]|novedades|corre[cç][a-z]*|"
    r"melhorias|vers[aã]o|adicionad[ao]|mudan[cç]as|am[eé]lior[a-z]*|"
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
PINNED_CONFIG_PATH = "pinned.json"
GITHUB_OWNERS_PATH = "githubOwners.json"
GITHUB_OWNER_CACHE_PATH = "githubOwnerCache.json"
GITHUB_OWNER_DISCOVERY_TTL_SECONDS = 24 * 60 * 60
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


def http_head_metadata(url, timeout=60):
    """Return status, size, ETag, and Last-Modified without the full body.

    Uses a ranged GET (bytes=0-0); servers that support ranges reply with
    "Content-Range: bytes 0-0/TOTAL", which gives the full size cheaply.
    """
    req = Request(quote_url(url), headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            cr = resp.headers.get("Content-Range", "")
            if cr and "/" in cr:
                length = cr.rsplit("/", 1)[1]
            else:
                length = resp.headers.get("Content-Length")
            return (
                resp.status,
                length,
                resp.headers.get("ETag"),
                resp.headers.get("Last-Modified"),
            )
    except (HTTPError, URLError, OSError):
        return None, None, None, None


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


def sha256_stream(url, timeout=120):
    """Stream-download url and return its digest, size, and HTTP validators."""
    req = Request(quote_url(url), headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    digest = hashlib.sha256()
    size = 0
    with urlopen(req, timeout=timeout) as resp:
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size, etag, last_modified


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


def _norm_channel_ru(channel):
    c = (channel or "").strip().lower()
    if c in ("dev", "alpha"):
        return "dev"
    if c in ("beta", "rcbeta"):
        return "beta"
    if c == "stable":
        return "stable"
    return "stable"


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

_MANIFEST_KEYS_ORDER = [
    "name", "summary", "description", "author", "url", "version",
    "changelog", "docFileName", "minimumNVDAVersion", "lastTestedNVDAVersion",
    "updateChannel",
]


def _parse_manifest(text):
    """Minimal manifest.ini reader (no configparser: values span raw lines)."""
    values = {}
    current = None
    buf = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", stripped)
        if m and not current:
            current = m.group(1)
            buf = [m.group(2)]
            continue
        if current:
            buf.append(stripped)
            # A line ending the multi-line value: next key or blank at top level
            if stripped and not stripped.startswith(("\"", "'")) and "=" in stripped \
                    and re.match(r"^[A-Za-z_]", stripped):
                # looks like a new key got swallowed; treat conservatively
                pass
    if current:
        values[current] = "\n".join(buf).strip()
    return values


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
        addon_id = spec.get("addon_id")
        if not repo or not addon_id:
            message = f"pinned entry missing repo/addon_id: {spec!r}"
            log(message)
            failures.append(message)
            continue
        try:
            entries.extend(_fetch_one_pinned(spec, repo, addon_id))
        except Exception as exc:  # noqa: BLE001 - report every failed pin together
            message = f"pinned entry {repo} failed: {exc}"
            log(message)
            failures.append(message)
    if failures:
        raise RuntimeError(
            "refusing to publish an incomplete pinned add-on set: "
            + "; ".join(failures)
        )
    return entries


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
    raw = http_get(asset["browser_download_url"], headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/octet-stream",
    })
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        manifest_text = zf.read("manifest.ini").decode("utf-8")
        names = zf.namelist()

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
    patched = _repack_addon(zf_bytes=None, raw=raw, names=names,
                            new_manifest=new_manifest.encode("utf-8"))

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


def _repack_addon(zf_bytes, raw, names, new_manifest):
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
    repos = _github_json(
        f"{GITHUB_API}/users/{encoded_login}/repos?per_page=100&type=owner"
    )
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


def _github_release_asset_candidates(repo):
    """Select the newest stable and prerelease asset for every filename family."""
    candidates, _state = _github_release_asset_state(repo)
    return candidates


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


def _github_owner_asset_candidates(owner_specs, batch_size=4):
    """Discover release assets for many users/organizations in batched queries."""
    specs_by_login = {
        spec["login"].casefold(): spec
        for spec in owner_specs
        if isinstance(spec, dict) and spec.get("login")
    }
    repos_by_login = {login: [] for login in specs_by_login}
    cursors = {login: None for login in specs_by_login}
    pending = list(specs_by_login)

    selection = """
repositories(first:100%s) {
  nodes {
    nameWithOwner
    isFork
    releases(first:20, orderBy:{field:CREATED_AT,direction:DESC}) {
      nodes {
        isDraft
        isPrerelease
        tagName
        publishedAt
        description
        releaseAssets(first:20) { nodes { name downloadUrl size updatedAt } }
      }
    }
  }
  pageInfo { hasNextPage endCursor }
}
"""

    while pending:
        next_pending = []
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset:offset + batch_size]
            fields = []
            alias_to_login = {}
            for index, login in enumerate(batch):
                alias = f"owner{index}"
                alias_to_login[alias] = login
                cursor = cursors[login]
                after = f",after:{json.dumps(cursor)}" if cursor else ""
                user_selection = selection % (f",ownerAffiliations:OWNER{after}")
                org_selection = selection % after
                fields.append(
                    f"{alias}:repositoryOwner(login:{json.dumps(specs_by_login[login]['login'])})"
                    " { ... on User { " + user_selection
                    + " } ... on Organization { " + org_selection + " } }"
                )
            data = _github_graphql("query {" + "\n".join(fields) + "}")
            for alias, login in alias_to_login.items():
                owner = data.get(alias)
                if owner is None:
                    raise RuntimeError(
                        f"configured GitHub account does not exist: {specs_by_login[login]['login']}"
                    )
                repositories = owner.get("repositories") or {}
                repos_by_login[login].extend(repositories.get("nodes") or [])
                page_info = repositories.get("pageInfo") or {}
                if page_info.get("hasNextPage"):
                    cursors[login] = page_info.get("endCursor")
                    next_pending.append(login)
        pending = next_pending

    candidates = []
    for login, repositories in repos_by_login.items():
        spec = specs_by_login[login]
        excluded = {
            name.strip().casefold()
            for name in spec.get("exclude_repositories", [])
            if isinstance(name, str) and name.strip()
        }
        exclude_forks = spec.get("fork_policy") == "exclude"
        repositories = [
            repo for repo in repositories
            if (
                repo.get("nameWithOwner", "").split("/", 1)[-1].casefold()
                not in excluded
                and not (exclude_forks and repo.get("isFork"))
            )
        ]
        discovered = {
            repo.get("nameWithOwner")
            for repo in repositories
            if repo.get("nameWithOwner")
        }
        known = {
            f"{spec['login']}/{name.strip()}"
            for name in spec.get("repositories", [])
            if isinstance(name, str) and name.strip()
        }
        missing = {name.casefold() for name in known} - {
            name.casefold() for name in discovered
        }
        if missing:
            raise RuntimeError(
                f"GitHub owner {spec['login']} is missing configured repositories: "
                + ", ".join(sorted(missing))
            )
        owner_count = 0
        for repo in repositories:
            repo_name = repo.get("nameWithOwner")
            if not repo_name:
                continue
            releases = (repo.get("releases") or {}).get("nodes") or []
            selected = _release_asset_candidates_from_records(repo_name, releases)
            candidates.extend(selected)
            owner_count += len(selected)
        log(
            f"GitHub owner {spec['login']}: {len(repositories)} repositories, "
            f"{owner_count} NVDA release asset candidates"
        )
    return candidates


def _github_owner_repository_names(owner_specs, batch_size=8):
    """Discover repository names and the parent of every fork."""
    specs_by_login = {
        spec["login"].casefold(): spec
        for spec in owner_specs
        if isinstance(spec, dict) and spec.get("login")
    }
    repositories = set()
    fork_parents = {}
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
                    raise RuntimeError(
                        f"configured GitHub account does not exist: "
                        f"{specs_by_login[login]['login']}"
                    )
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
                repositories.update(discovered_repositories - scanned_repositories)
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
    failures = []
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
                failures.append(f"{source_name}: {exc}")
    if failures:
        raise RuntimeError(
            "refusing to publish incomplete GitHub author discovery: "
            + "; ".join(failures)
        )

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
        cache_key = item.get("cache_key") or url
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
                new_cache[item.get("cache_key") or item["download_url"]] = entry
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
                new_cache[item.get("cache_key") or item["download_url"]] = {
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
                new_cache[item.get("cache_key") or item["download_url"]] = {
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
                "channel": _norm_channel_ru(link.get("channel")),
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
    version = (entry.get("version") or "").strip()
    download_url = (entry.get("download_url") or "").strip()
    source = entry.get("source")

    if not name or name.lower() in _TEMPLATE_NAMES:
        return "missing or template add-on id"
    if entry.get("category") == "synth-voice":
        return "voice/data pack (skipped)"
    if entry.get("subcategory") in ("vosk", "silero", "vosk_tts"):
        return "voice/data model (skipped)"
    # Official store entries are NV Access-reviewed with upstream-computed
    # hashes; their version strings are already store-valid, so only the
    # community catalogs go through the lenient sanitizer check.
    if source != "official" and sanitize_version(version) is None:
        return f"unparseable version {version!r}"
    if not download_url:
        return "no download_url"
    return None


#: Static English translations for add-ons whose only available summary /
#: description is not English. Keyed by addonId. See translations.json.
TRANSLATIONS_PATH = "translations.json"
TRANSLATIONS = {}


def load_translations(path=TRANSLATIONS_PATH):
    """Load the static English translation map, if present. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("translations", data)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return {}


def _translate_entry(entry):
    """Overlay configured English metadata onto a catalog entry."""
    tr = TRANSLATIONS.get(entry["name"])
    if tr:
        for field in ("summary", "description", "author", "changelog"):
            if tr.get(field):
                entry[field] = tr[field]

    changelog = entry.get("changelog") or ""
    if (
        _NON_LATIN_SCRIPT_RE.search(changelog)
        or _NON_ENGLISH_LATIN_CHANGELOG_RE.search(changelog)
        or entry["name"] in _KNOWN_NON_ENGLISH_CHANGELOG_IDS
    ):
        entry["changelog"] = "Release notes are not available in English."


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
        "displayName": clean_text(entry.get("summary")) or name,
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
        "storeSource": STORE_SOURCE_LABELS.get(
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
    """
    priority = {
        "pinned": 5,
        "github_owner": 4,
        "official": 3,
        "ru": 2,
        "bestmidi": 1,
        "es": 0,
    }
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
        result.append(winner)
    return result


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
    year = re.search(r"version_year\s*=\s*(\d+)", src)
    major = re.search(r"version_major\s*=\s*(\d+)", src)
    minor = re.search(r"version_minor\s*=\s*(\d+)", src)
    if not (year and major and minor):
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
    except (HTTPError, URLError, OSError):
        return FALLBACK_BACK_COMPAT_TO
    m = re.search(r"BACK_COMPAT_TO\s*[:=]\s*\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)", src)
    if not m:
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


def published_nvda_api_versions(data):
    """Select every Add-on Store-era API version for publication."""
    released = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ver = _api_version_from_entry(entry)
        if ver is not None and ver >= ADDON_STORE_FIRST_API_VERSION:
            released.add(ver)
    return [
        f"{ver[0]}.{ver[1]}.{ver[2]}"
        for ver in sorted(released, reverse=True)
    ]


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
):
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

    for lang in locales:
        for channel in channels:
            # "latest.json" is NVDA's "include incompatible add-ons" view: the
            # full catalog. The per-apiVersion files are the "compatible" view
            # and must only contain add-ons compatible with that API version.
            write_bytes(f"{lang}/{channel}/latest.json", canonical_bytes)
            for ver in api_versions:
                if ver == "latest":
                    continue
                write_bytes(f"{lang}/{channel}/{ver}.json", compatible_bytes[ver])


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
    parser.add_argument("--channels", help="comma-separated channel override")
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
        api_versions = published_nvda_api_versions(nvda_api_entries) + ["latest"]
        current = current_nvda_api_version()
        if current and current not in api_versions:
            api_versions.insert(0, current)
    api_versions = list(dict.fromkeys(api_versions))

    hashcache = load_hashcache(args.hashcache)
    new_hashcache = dict(hashcache)

    # 1. Fetch and normalize from each source.
    all_entries = []
    rejected = []

    if "official" in sources:
        log("Fetching official NV Access add-on store (Chinese mirror failover)")
        entries = fetch_official()
        log(f"official: {len(entries)} add-ons")
        all_entries.extend(entries)

    if "bestmidi" in sources:
        log(f"Fetching {BESTMIDI_URL}")
        entries = fetch_bestmidi()
        log(f"bestmidi: {len(entries)} add-ons")
        all_entries.extend(entries)

    if "ru" in sources:
        log(f"Fetching {RU_ADDONS_URL}")
        entries = fetch_ru()
        log(f"nvda-addons.ru: {len(entries)} add-ons")
        all_entries.extend(entries)

    if "es" in sources:
        log("Fetching nvda.es add-on catalog (nvda-addons.org failover)")
        entries = fetch_es()
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

    if args.limit:
        all_entries = all_entries[: args.limit]

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

    # 4. Download + hash (with persistent, resumable cache).
    cache_lock = threading.Lock()
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
        if e.get("source") == "pinned":
            patched = e.pop("_patched_bytes")
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
            digest = hashlib.sha256(url.encode()).hexdigest()
            with cache_lock:
                new_hashcache[url] = {"sha256": digest, "size": 0}
            return e, digest, 0, None

        cached = hashcache.get(url)
        if cached and not args.no_head_check:
            status, length, etag, last_modified = http_head_metadata(url)
            size_matches = (
                status in (200, 206)
                and length is not None
                and str(cached.get("size")) == str(length)
            )
            cached_etag = cached.get("etag")
            cached_modified = cached.get("last_modified")
            if cached_etag and etag:
                validators_match = cached_etag == etag
            elif cached_modified and last_modified:
                validators_match = cached_modified == last_modified
            else:
                validators_match = not any(
                    (cached_etag, etag, cached_modified, last_modified)
                )
            cached_version = cached.get("version")
            version_matches = cached_version == e.get("version")
            migrating_legacy_cache = cached_version is None and not any(
                (cached_etag, cached_modified)
            )
            if size_matches and (validators_match or migrating_legacy_cache) and (
                version_matches or migrating_legacy_cache
            ):
                refreshed_cache = dict(cached)
                refreshed_cache.update({
                    "etag": etag,
                    "last_modified": last_modified,
                    "version": e.get("version"),
                })
                with cache_lock:
                    new_hashcache[url] = refreshed_cache
                return e, cached["sha256"], cached["size"], None

        try:
            digest, size, etag, last_modified = sha256_stream(url)
            with cache_lock:
                new_hashcache[url] = {
                    "sha256": digest,
                    "size": size,
                    "etag": etag,
                    "last_modified": last_modified,
                    "version": e.get("version"),
                }
            return e, digest, size, None
        except (HTTPError, URLError, OSError) as exc:
            return e, None, 0, str(exc)

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
        if e.get("source") == "pinned":
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

    # Hash the canonical catalog plus every compatibility-filtered view, so any
    # change to the catalog OR the filter bumps the hash and forces NVDA clients
    # to re-fetch the affected endpoint.
    hash_input = canonical_bytes
    for ver in api_versions:
        if ver == "latest":
            continue
        hash_input += b"\x00" + compatible_bytes[ver]
    cache_hash = hashlib.sha256(hash_input).hexdigest()

    stats = {
        "accepted": len(output),
        "rejected": len(rejected),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api_versions": api_versions,
        "back_compat_to": back_compat_by_ver,
        "compatible_counts": {v: len(json.loads(b)) for v, b in compatible_bytes.items()},
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
    )

    # Persist the hash cache so the next run only re-downloads changed add-ons.
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "hashcache.json"), "w", encoding="utf-8") as f:
        json.dump(new_hashcache, f)
    if os.path.exists(GITHUB_OWNER_CACHE_PATH):
        with open(GITHUB_OWNER_CACHE_PATH, "rb") as source_cache:
            with open(os.path.join(args.out, GITHUB_OWNER_CACHE_PATH), "wb") as public_cache:
                public_cache.write(source_cache.read())
    flush_cache()

    total_bytes = sum(s for _, _, s in results)
    log(
        f"Done: {stats['accepted']} add-ons, {total_bytes/1024/1024:.1f} MiB hashed, "
        f"cacheHash={cache_hash}"
    )
    if rejected:
        log(f"Rejected {len(rejected)} candidates (see {args.out}/rejected.json)")


if __name__ == "__main__":
    main()
