# NVDA Add-on Update Mirror — Current Design

## Goal

Publish a self-updating, English-metadata mirror in the wire format consumed by
NVDA's built-in Add-on Store. Known upstream add-ons are checked every ten
minutes and deployed through GitHub Pages.

## Sources

- NV Access's official `addon-datastore` catalog, with its hashes and scan data.
- BestMidi's add-on catalog.
- `nvda-addons.ru` community catalog.
- `nvda.es`, with `nvda-addons.org` as failover; only original add-ons not
  already covered by a valid stronger source in the same channel are retained.
- Direct `.nvda-addon` release assets from owners in `githubOwners.json`.
- Explicit variants and packages in `pinned.json`.

Priority within an add-on ID and channel is: pinned, direct author, official,
Russian catalog, BestMidi, then Spanish catalog. A newer version from the same
source wins.

## Direct-author discovery

An asset is considered only when its filename ends in `.nvda-addon`. New or
changed assets are downloaded and accepted only when they are readable ZIP
files with a root `manifest.ini` and a non-template add-on ID. Invalid bundles,
removed assets, and unrelated repositories are recorded as rejections.

`githubOwnerCache.json` stores validated manifests, the repository baseline,
and GitHub ETags. Every ten-minute run conditionally checks all known add-on
repositories. Once per day a lightweight owner scan discovers newly created
repositories; only repositories not previously examined receive a baseline
release scan.

## Pipeline

1. Fetch and normalize all enabled sources.
2. Reject structurally invalid candidates before cross-source suppression.
3. Retain Spanish originals only when the same add-on/channel is not covered by
   a valid stronger source.
4. Deduplicate by case-insensitive add-on ID and channel.
5. Use upstream hashes where trusted; otherwise stream the package and compute
   SHA-256. Cache version, size, ETag, and Last-Modified so same-URL and
   same-size replacements are detected.
6. Overlay maintained English metadata. Non-English release notes without an
   English source receive a clear English unavailable message.
7. Emit `addons.json`, `cacheHash.json`, rejection reports, compatibility-filtered
   API-version files, and `latest.json` for every supported locale.
8. Deploy the generated static site through GitHub Pages.

## Scheduling and reliability

The workflow has an offset ten-minute cron and a parallel self-dispatch
watchdog. Download socket stalls are capped at two minutes. Source-wide network
or API failures stop publication so a partial catalog cannot replace a complete
deployment. Permanent invalid or removed individual release assets are rejected
with an auditable reason.

## Verification

- `python -m py_compile mirror.py build_helper.py`
- `python -m unittest discover -s tests -v`
- Validate `githubOwners.json` and `translations.json` with `json.tool`.
- Run a full-source English smoke build and parse every generated JSON file.
- Confirm a second GitHub-owner scan uses conditional 304 responses and does
  not materially consume the hourly API quota.
- Inspect the helper archive to ensure it contains only `manifest.ini` and the
  global plugin source and never calls `dataManager.initialize()`.
