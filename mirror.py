#!/usr/bin/env python3
"""Build a NVDA Add-on Store mirror from multiple upstream catalogs.

Sources:
- https://bestmidi.com/addons/addons.json  (GitHub-discovered "bleeding edge" list)
- https://nvda-addons.ru/get.php?addonslist (Russian community catalog, many
  non-GitHub add-ons; the same JSON the TiendaNVDA/Store add-ons consume)

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
import zipfile
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit

BESTMIDI_URL = "https://bestmidi.com/addons/addons.json"
RU_ADDONS_URL = "https://nvda-addons.ru/get.php?addonslist"
NVDA_BUILD_VERSION_URL = (
    "https://raw.githubusercontent.com/nvaccess/nvda/master/source/buildVersion.py"
)

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

# Recent NVDA API versions to expose. "latest" always resolves the "show all
# (incompatible)" view; the numbered entries cover the default "compatible"
# view for recent NVDA releases. The current dev version is prepended at build
# time. Kept deliberately short: Pages forbids symlinks, so every path is a
# real copy and 73 locales multiply it -- 3 copies/locale keeps the published
# site near 750 MB of the 1 GB Pages warning as the catalog grows.
CURATED_API_VERSIONS = [
    "2026.2.0",
]

# API version regex mirrors NVDA source/addonAPIVersion.py: year.major(.minor)
_API_VERSION_RE = re.compile(r"^(0|\d{4})\.(\d)(?:\.(\d))?$")

_INT_RUN_RE = re.compile(r"\d+")

#: Cyrillic block, used to detect Russian (nvda-addons.ru) text so the store
#: can prefer English where an English sibling source exists.
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")

_TEMPLATE_NAMES = {"addontemplate", "__addon_id__"}

USER_AGENT = (
    "Mozilla/5.0 (compatible; nvda-addon-mirror/1.0; +https://github.com/"
    "serrebidev/nvda-addon-mirror)"
)

ALL_SOURCES = ("official", "bestmidi", "ru", "pinned")
PINNED_CONFIG_PATH = "pinned.json"
GITHUB_API = "https://api.github.com"

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


def http_get_json(url, timeout=120):
    return json.loads(http_get(url, timeout=timeout).decode("utf-8"))


def http_head_length(url, timeout=60):
    """Return (status, total_size) without downloading the full body.

    Uses a ranged GET (bytes=0-0); servers that support ranges reply with
    "Content-Range: bytes 0-0/TOTAL", which gives the full size cheaply.
    """
    req = Request(quote_url(url), headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            cr = resp.headers.get("Content-Range", "")
            if cr and "/" in cr:
                return resp.status, cr.rsplit("/", 1)[1]
            return resp.status, resp.headers.get("Content-Length")
    except (HTTPError, URLError, OSError):
        return None, None


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


def sha256_stream(url, timeout=3600):
    """Stream-download url and return (hex_sha256, size_bytes)."""
    req = Request(quote_url(url), headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    digest = hashlib.sha256()
    size = 0
    with urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


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
    for spec in pinned:
        repo = spec.get("repo")
        addon_id = spec.get("addon_id")
        if not repo or not addon_id:
            log(f"pinned entry missing repo/addon_id: {spec!r}")
            continue
        try:
            entries.extend(_fetch_one_pinned(spec, repo, addon_id))
        except Exception as exc:  # noqa: BLE001 - one bad pinned entry must not kill the build
            log(f"pinned entry {repo} failed: {exc}")
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
    version = (asset["name"].rsplit(".nvda-addon", 1)[0] or release["tag_name"]).strip()
    # Prefer the manifest version if it embeds the tag (e.g. 19.1.3-RS)
    mv = _manifest_value(manifest_text, "version")
    if mv:
        version = mv
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


def fetch_bestmidi():
    data = http_get_json(BESTMIDI_URL)
    entries = []
    for a in data.get("addons", []):
        name = (a.get("name") or "").strip()
        download_url = (a.get("download_url") or "").strip()
        version = (a.get("version") or "").strip()
        # bestmidi's version field is sometimes "Unknown" (or otherwise
        # unparseable) even though the release asset filename carries the real
        # version. Recover it from the filename so a valid, downloadable
        # add-on is not rejected for a missing version string.
        if sanitize_version(version) is None:
            recovered = _version_from_filename(a.get("download_name"))
            if recovered is not None:
                version = recovered
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
#: description is Russian (nvda-addons.ru). Keyed by addonId. See
#: translations.json.
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
    """Overlay English translations onto an entry's summary/description."""
    tr = TRANSLATIONS.get(entry["name"])
    if not tr:
        return
    if tr.get("summary"):
        entry["summary"] = tr["summary"]
    if tr.get("description"):
        entry["description"] = tr["description"]


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

    Preference order within the same (addonId, channel): official (NV
    Access-reviewed, VirusTotal data, upstream hash) > nvda-addons.ru
    (curated, direct links) > bestmidi. Within a single source, the first
    occurrence wins; an entry with a download URL beats one without.

    When the winning entry's text is Russian, English summary/description/
    changelog are adopted from a non-Cyrillic sibling (official first, then
    bestmidi), so the store shows English wherever an English source exists
    while keeping the winner's reliable download URL and hash.
    """
    priority = {"official": 2, "ru": 1, "bestmidi": 0}
    by_key = {}
    for e in entries:
        key = (e["name"], e.get("channel") or "stable")
        by_key.setdefault(key, []).append(e)

    result = []
    for group in by_key.values():
        winner = max(
            group,
            key=lambda e: (1 if e["download_url"] else 0, priority.get(e["source"], 0)),
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
        "<a href='https://nvda-addons.ru/'>nvda-addons.ru</a>.</p>"
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

    def write_path(rel_path):
        # GitHub Pages rejects artifacts containing symlinks, and the artifact
        # upload follows them anyway (ballooning size), so always write real
        # copies of the canonical JSON.
        target = os.path.join(out_dir, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(canonical_bytes)

    for lang in locales:
        for channel in channels:
            write_path(f"{lang}/{channel}/latest.json")
            for ver in api_versions:
                write_path(f"{lang}/{channel}/{ver}.json")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build a NVDA add-on store mirror.")
    parser.add_argument("--out", default="public")
    parser.add_argument("--sources", default=",".join(ALL_SOURCES),
                        help="comma-separated sources: bestmidi,ru")
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

    api_versions = list(CURATED_API_VERSIONS) + ["latest"]
    if args.api_versions:
        api_versions = args.api_versions.split(",")
    else:
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
            status, length = http_head_length(url)
            if status in (200, 206) and length is not None and str(cached.get("size")) == str(length):
                with cache_lock:
                    new_hashcache[url] = cached
                return e, cached["sha256"], cached["size"], None

        try:
            digest, size = sha256_stream(url)
            with cache_lock:
                new_hashcache[url] = {"sha256": digest, "size": size}
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

    canonical_bytes = json.dumps(
        output, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    cache_hash = hashlib.sha256(canonical_bytes).hexdigest()

    stats = {
        "accepted": len(output),
        "rejected": len(rejected),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api_versions": api_versions,
        "sources": sources,
    }

    emit(
        args.out,
        canonical_bytes,
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
