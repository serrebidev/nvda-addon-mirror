# NVDA Add-on Update Mirror — Implementation Plan

## Goal

Build a GitHub-hosted, self-updating mirror of the "Bleeding Edge" add-on directory at
`https://bestmidi.com/addons/`, published in the exact wire format NVDA's Add-on Store
client consumes, refreshed every 12 hours. Users point NVDA at our GitHub Pages URL to
browse/download/update all add-ons bestmidi tracks — minus "rejected candidates".

## What the sources actually are (verified)

### bestmidi.com/addons/ (the upstream we mirror)
- It is a JS front-end backed by one static file: `https://bestmidi.com/addons/addons.json`.
- `addons.json` is **already the accepted/curated list**. Structure:
  `{ generated_at, disclaimer, search, addons: [ ... ] }`.
- Each entry: `name` (== addonId), `summary`, `description`, `version`,
  `minimum_nvda_version`, `last_tested_nvda_version`, `author`, `owner`,
  `homepage_url`, `source_url`, `update_channel`, `addon_license`,
  `addon_license_url`, `changelog`, `download_url`, `download_name`,
  `download_size_bytes`, `metadata_path`, etc.
- There is **no public "rejected candidates" endpoint**. `changelog-history.json` is the
  raw discovery feed (contains junk: template repos with version `x.y`, name
  `addonTemplate` / `__ADDON_ID__`). `addons.json` is the post-curation result.

**Decision (from user): "rejected candidates" = bestmidi's acceptance criteria.** We use
`addons.json` as the source of truth and additionally drop any entry that cannot satisfy
NVDA's hard requirements (below). This is the "track all add-ons except rejected candidates"
rule, enforced deterministically.

### NVDA add-on store format (verified against NVDA master)
From `source/addonStore/network.py`, `dataManager.py`, `models/addon.py`, `models/version.py`:

URL layout NVDA requests (`baseServerURL` is user-configurable):
- `{base}/{lang}/{channel}/{apiVersion}.json`  → flat JSON array of add-ons
  - `lang` = NVDA locale code, underscore form (e.g. `en`, `pt_BR`, `zh_CN`)
  - `channel` ∈ `all` | `stable` | `beta` | `dev` | `external`  (default requested: `all`)
  - `apiVersion` = `latest` (the "show all versions / incompatible" view) or
    `{year}.{major}.{minor}` from `addonAPIVersion.CURRENT` (e.g. `2026.1.1`)
- `{base}/cacheHash.json` → a JSON-encoded **string** (parsed via `response.json()`); drives
  NVDA's re-fetch decision.

Per-add-on object required keys (from `_createStoreModelFromData`):
```
addonId            : str
displayName        : str
description        : str
publisher          : str
channel            : "stable"|"beta"|"dev"|"external"
addonVersionName   : str
addonVersionNumber : {major:int, minor:int, patch:int}   # 2–3 int parts, patch defaults 0
homepage           : str | null
changelog          : str | null
license            : str
licenseURL         : str | null
sourceURL          : str
URL                : str          # direct .nvda-addon download
sha256             : str          # lowercase hex; NVDA verifies on download
minNVDAVersion     : {major,minor,patch}
lastTestedVersion  : {major,minor,patch}
reviewURL          : str | null
submissionTime     : int | null   # epoch ms
legacy             : bool         # default false
```

Two hard constraints this imposes:
1. **`sha256` is enforced** — `AddonFileDownloader._checkChecksum` compares the hash of the
   downloaded file (casefolded) to `addon.sha256`. `addons.json` provides no hash, so the
   build **must download each `.nvda-addon` and compute its SHA-256**. (Sizes vary widely —
   e.g. nokiaKlatt ≈ 129 MB, VisionAssistant ≈ 39 MB — so downloads are streamed and cached.)
2. **Version tuples are integers, 2–3 parts** — `MajorMinorPatch._parseVersionFromVersionStr`
   raises `ValueError` otherwise. bestmidi versions like `2026.08.13` are fine; `Unknown`,
   `x.y`, `addonTemplate` and non-numeric tags are not → such entries must be rejected.

## Filter rules ("rejected candidates" → dropped)

An entry in `addons.json` is **rejected** (skipped) when any of:
- `download_url` is empty/missing (cannot hash → cannot be safely installed), or
- `version` is `Unknown`, empty, or does not parse to 2–3 integer parts, or
- `name` is empty or a template placeholder (`addonTemplate`, `__ADDON_ID__`), or
- `minimum_nvda_version` / `last_tested_nvda_version` do not parse to a valid version
  (fall back to a safe default rather than dropping, when only these are malformed).

Everything else is included. This is deterministic and re-runs produce a stable result.

## Architecture

Single Python script + one GitHub Actions workflow. No server, no database.

### Repo layout
```
.github/workflows/update.yml   # cron + manual dispatch
mirror.py                      # fetch → filter → download+hash → transform → emit
requirements.txt               # requests (only dependency)
rejected.txt                   # generated: rejected addonId -> reason (audit trail)
public/                        # generated, committed to gh-pages (the served site)
  cacheHash.json
  {lang}/all/latest.json
  {lang}/all/{apiVersion}.json     # for each supported API version
  {lang}/stable/latest.json ...    # stable/beta/dev mirrors (same content)
  index.html                    # small landing page (optional)
README.md
```

### `mirror.py` steps
1. **Fetch upstream** `https://bestmidi.com/addons/addons.json`.
2. **Fetch NVDA metadata** (for file layout, not add-on data):
   - current API version from NVDA `source/addonAPIVersion.py` (or a bundled list), and
   - supported locale codes from NVDA `source/localeData.py` (fallback: hardcoded list).
3. **Filter** per the rules above; write `rejected.txt` with reasons.
4. **Download & hash** each included add-on: stream `download_url`, compute SHA-256,
   record size. Use a disk cache keyed by URL so unchanged add-ons are not re-downloaded
   on every run (cache restored via `actions/cache`).
5. **Transform** each entry to the NVDA schema:
   - `addonId = name`; `displayName = summary`; `description = description`;
     `publisher = author` (fallback `owner`); `channel` from `update_channel`
     (`dev`/`beta` when set, else `stable`); `addonVersionName = version`;
     `addonVersionNumber`/`minNVDAVersion`/`lastTestedVersion` from parsed versions;
     `license`/`licenseURL` from `addon_license`/`addon_license_url`; `sourceURL`,
     `homepage`, `changelog`, `URL = download_url`, `sha256` from step 4;
     `submissionTime` = epoch ms of `last_pushed` (or `created_at`).
6. **Emit**:
   - One canonical flat array; write it to `{lang}/{channel}/{apiVersion}.json` and
     `{lang}/{channel}/latest.json` for every `lang` × `channel` combination
     (identical content; NVDA filters client-side).
   - `cacheHash.json` = JSON-encoded string hash of the canonical array
     (changes only when the add-on set/hashes change → NVDA re-fetches only then).
   - `index.html` + `README.md` pointer.
7. **Publish**: commit `public/` to the `gh-pages` branch (using
   `peaceiris/actions-gh-pages` or a push step).

### GitHub Actions (`update.yml`)
- Triggers: `schedule: cron '0 */12 * * *'` and `workflow_dispatch`.
- Runs on `ubuntu-latest`, Python 3.13, `pip install -r requirements.txt`,
  restore `actions/cache` (download cache) → `python mirror.py` → publish to `gh-pages`.
- Enable **GitHub Pages** → source = `gh-pages` branch. Mirror URL:
  `https://<owner>.github.io/<repo>/`.

## Hosting & consumption

- Repo: `serrebidev/nvda-addon-mirror` (name TBD).
- Serve via GitHub Pages. Static `.json` files get correct `application/json` MIME.
- Users set NVDA Add-on Store base URL to `https://serrebidev.github.io/nvda-addon-mirror/`
  either by editing NVDA config `[addonStore] baseServerURL = ...` or via a tiny helper
  add-on (optionally modeled on `nvdacn/NVDAUpdateMirror`).

## Known risks / notes
- **Bandwidth/storage**: full re-hash downloads a few hundred MB per run. Mitigated by
  `actions/cache` keyed on URL (unchanged add-ons skipped). Large assets are streamed.
- **No human vetting**: bestmidi is explicitly untested add-ons; our mirror inherits that
  (stated in README/index). `sha256` still guarantees immutability of what is downloaded.
- **Version-parse failures** are the main source of rejected entries; `rejected.txt` keeps
  them auditable.
- **API-version files**: NVDA requests `{current}.json`; we generate files for a small
  recent list (latest stable + a few prior) plus `latest.json`, so older NVDA clients still
  resolve. List is updated as NVDA ships.

## Verification
- Unit-ish smoke test of `mirror.py` transform + filter on a fixture JSON.
- Manually run once, then curl the published `cacheHash.json` and
  `en/all/latest.json`, confirm they parse and each object has a non-empty `sha256`.
- Point a local NVDA config at the Pages URL and confirm the Add-on Store lists add-ons.
