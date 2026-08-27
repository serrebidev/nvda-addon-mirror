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
import hashlib
import json
import os
import re
import threading
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
# real ~2.6 MB copy, and 73 locales multiply it -- 4 copies/locale keeps the
# published site under the 1 GB Pages warning.
CURATED_API_VERSIONS = [
    "2026.2.0", "2026.1.1",
]

# API version regex mirrors NVDA source/addonAPIVersion.py: year.major(.minor)
_API_VERSION_RE = re.compile(r"^(0|\d{4})\.(\d)(?:\.(\d))?$")

_INT_RUN_RE = re.compile(r"\d+")

_TEMPLATE_NAMES = {"addontemplate", "__addon_id__"}

USER_AGENT = (
    "Mozilla/5.0 (compatible; nvda-addon-mirror/1.0; +https://github.com/"
    "serrebidev/nvda-addon-mirror)"
)

ALL_SOURCES = ("bestmidi", "ru")


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


def fetch_bestmidi():
    data = http_get_json(BESTMIDI_URL)
    entries = []
    for a in data.get("addons", []):
        name = (a.get("name") or "").strip()
        download_url = (a.get("download_url") or "").strip()
        entries.append(
            {
                "name": name,
                "summary": clean_text(a.get("summary")),
                "description": clean_text(a.get("description")),
                "author": (a.get("author") or "").strip() or (a.get("owner") or "").strip(),
                "version": (a.get("version") or "").strip(),
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

    if not name or name.lower() in _TEMPLATE_NAMES:
        return "missing or template add-on id"
    if entry.get("category") == "synth-voice":
        return "voice/data pack (skipped)"
    if entry.get("subcategory") in ("vosk", "silero", "vosk_tts"):
        return "voice/data model (skipped)"
    if sanitize_version(version) is None:
        return f"unparseable version {version!r}"
    if not download_url:
        return "no download_url"
    return None


def transform(entry, sha256):
    """Map a normalized entry to the NVDA add-on store object."""
    name = entry["name"]
    version = entry["version"]

    addon_version = sanitize_version(version)
    min_nvda = entry.get("min_nvda") or (0, 0, 0)
    last_tested = entry.get("last_tested") or (0, 0, 0)

    author = entry.get("author") or "Unknown"
    license_name = entry.get("license") or "Unknown"
    license_url = entry.get("license_url") or None
    homepage = entry.get("homepage") or None
    source_url = entry.get("source_url") or entry.get("homepage") or None

    return {
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
        "homepage": homepage,
        "changelog": entry.get("changelog") or None,
        "license": license_name,
        "licenseURL": license_url,
        "sourceURL": source_url,
        "URL": entry.get("download_url") or "",
        "sha256": sha256,
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
        "reviewURL": None,
        "submissionTime": entry.get("submission_ms"),
        "legacy": False,
    }


def dedupe(entries):
    """Dedupe by addonId. Entries with a download_url win over ones without;
    within a source, first occurrence wins. On cross-source collision, the
    nvda-addons.ru entry is preferred (curated catalog, direct download links)."""
    by_name = {}
    for e in entries:
        name = e["name"]
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = e
            continue
        # Prefer an entry with a download_url.
        if not existing["download_url"] and e["download_url"]:
            by_name[name] = e
            continue
        if existing["download_url"] and not e["download_url"]:
            continue
        # Prefer the ru catalog on ties (both have download URLs).
        if e["source"] == "ru" and existing["source"] != "ru":
            by_name[name] = e
    return list(by_name.values())


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


def emit(
    out_dir,
    canonical_bytes,
    cache_hash,
    api_versions,
    locales,
    channels,
    stats,
    rejected,
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

    index_html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>NVDA Add-on Mirror</title></head><body>"
        "<h1>NVDA Add-on Store Mirror</h1>"
        f"<p>{stats['accepted']} add-ons mirrored from "
        "<a href='https://bestmidi.com/addons/'>bestmidi.com/addons/</a> and "
        "<a href='https://nvda-addons.ru/'>nvda-addons.ru</a>.</p>"
        "<p>Set the NVDA Add-on Store base URL to this site to use it. "
        "These add-ons are untested; install at your own risk.</p>"
        "</body></html>"
    )
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

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
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()

    sources = args.sources.split(",")
    locales = args.locales.split(",") if args.locales else LOCALES
    channels = args.channels.split(",") if args.channels else CHANNELS

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

    # 5. Transform.
    output = []
    for e, digest, size in results:
        output.append(transform(e, digest))

    rejected.extend(download_failed)
    rejected.sort(key=lambda r: (r.get("source") or "", r.get("addonId") or ""))

    output.sort(key=lambda o: o["addonId"])

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
