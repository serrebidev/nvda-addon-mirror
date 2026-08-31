# NVDA Add-on Update Mirror

A self-updating mirror of NVDA add-on catalogs, direct author releases, and
explicitly pinned GitHub releases, published in the exact wire format NVDA's built-in Add-on Store
consumes. It refreshes every 10 minutes via GitHub Actions and is served from
GitHub Pages.

Sources:

- **[NV Access Add-on Store](https://github.com/nvaccess/addon-datastore)** —
  the official catalog, including its upstream hashes and VirusTotal results.
- **[bestmidi.com/addons/](https://bestmidi.com/addons/)** — the GitHub-discovered
  "bleeding edge" list (`addons.json`).
- **[nvda-addons.ru](https://nvda-addons.ru/)** — the Russian community catalog
  (`get.php?addonslist`, the same JSON its own TiendaNVDA/Store add-ons use),
  which hosts many add-ons that never publish GitHub releases (synthesizers,
  voice packs, localized forks, etc.).
- **[nvda.es](https://nvda.es/)** with
  **[nvda-addons.org](https://nvda-addons.org/)** as failover — these two domains
  serve the same Spanish-community catalog byte for byte. The mirror monitors
  it for original add-on IDs and drops aliases or add-ons already covered by a
  stronger source.
- **Configured GitHub authors** — release assets from the requested author list
  are accepted only when the filename ends in `.nvda-addon` and the downloaded
  ZIP has a valid root `manifest.ini`. Known add-on repositories are checked on
  every ten-minute run; a lightweight daily account scan discovers new repos.
  Original repositories are always eligible. A fork is eligible only when its
  numeric release version is strictly newer than its parent repository's
  release; equal, older, missing, or incomparable fork versions are rejected.
  An owner may exclude every fork; `serrebidev` does this because those forks
  are contribution/PR branches. The mirror therefore uses the original
  `keyang556/tdesktopnvda`, not `serrebidev/tdesktopnvda`.
  Explicitly pinned variants can opt into `fork_policy: include` when they are
  intentionally different products published under separate manifest IDs. The
  four release-bearing Eloquence 64 variants are pinned this way where needed,
  and are published with unique IDs and display names so installing one cannot
  overwrite another.

## What it does

1. Fetches every catalog (using nvda-addons.org only if nvda.es fails).
2. Rejects candidates that cannot be safely installed through NVDA's store:
   - no download URL,
   - missing / template add-on id,
   - a version string with no parseable numeric parts.
3. Merges sources case-insensitively by add-on id and channel. Explicitly pinned
   releases win, followed by direct author releases, the official store,
   nvda-addons.ru, bestmidi, and Spanish-catalog originals.
4. Downloads each remaining `.nvda-addon`, computes its SHA-256 (NVDA enforces
   this checksum on install), and emits the NVDA store schema.
5. Writes `cacheHash.json`, `addons.json`, and
   `{lang}/{channel}/{apiVersion}.json` for every NVDA locale, channel, and a
   curated set of recent API versions.
6. Publishes everything to GitHub Pages.

> These add-ons are **untested**. bestmidi's disclaimer: *"These add-ons have
> not been tested and this is not an official NVDA add-on repository."* The
> SHA-256 hash still guarantees immutability of what you download, but nothing
> here is audited.

## Using the mirror

The mirror serves **metadata only** — it does not re-host the `.nvda-addon`
files. Each entry's `URL` points at the original host (GitHub release or
`nvda.ru` upload), and NVDA downloads directly from there. The build downloads
each file only once to compute the SHA-256 checksum NVDA enforces on install.

Point NVDA's Add-on Store at the live mirror:

```
https://serrebidev.github.io/nvda-addon-mirror
```

Two ways to do this:

1. **Install the helper add-on** — latest build:
   [dist/addonStoreMirror-1.1.1.nvda-addon](dist/addonStoreMirror-1.1.1.nvda-addon)
   (raw link: https://raw.githubusercontent.com/serrebidev/nvda-addon-mirror/main/dist/addonStoreMirror-1.1.1.nvda-addon).
   It sets `[addonStore] baseServerURL` to the mirror on startup and restores it
   when disabled — the same mechanism
   [nvdacn/NVDAUpdateMirror](https://github.com/nvdacn/NVDAUpdateMirror) uses.
   Use 1.1.1 or later. Version 1.1.1 fixes a crash caused by replacing NVDA's
   live Add-on Store data manager; 1.0.0 had a trailing-slash URL bug.
2. **Edit `nvda.ini`** manually:
   ```ini
   [addonStore]
   baseServerURL = https://serrebidev.github.io/nvda-addon-mirror
   ```
   then restart NVDA.

## Browsing what was rejected

The site publishes [rejected.html](https://serrebidev.github.io/nvda-addon-mirror/rejected.html)
— every candidate excluded while building the mirror, grouped by reason
(voice/data packs skipped, no download URL, unparseable version, …), with an
in-page filter. The same data is available as JSON at `rejected.json`.

## Repo layout

- `mirror.py` — the whole pipeline (stdlib only, Python 3.11+).
- `.github/workflows/update.yml` — ten-minute cron plus a self-dispatching
  ten-minute keep-alive, because GitHub may delay or drop scheduled events.
- `helper/` — source of the `addonStoreMirror` helper add-on; `build_helper.py`
  packs it into `dist/`.
- `public/` — generated site (published to GitHub Pages by Actions).

## Running locally

```sh
python mirror.py --out public              # full build (all sources)
python mirror.py --sources ru --limit 6 --skip-download --locales en   # fast smoke test
```

The mirror writes real files rather than symlinks because GitHub Pages rejects
artifacts that contain symlinks.

## Notes and trade-offs

- **File layout**: NVDA requests `{base}/{lang}/{channel}/{apiVersion}.json`,
  using the language/channel/apiVersion only as cache keys — the returned list
  is identical for all of them. The `apiVersion` is the *running NVDA's own*
  add-on API version (e.g. `2026.2.0`), so the mirror must emit a file for
  every released NVDA version still in use or those users get a 404 and an
  empty "compatible" list.
  - **Old NVDA support has a hard floor of NVDA 2024.1**: the Add-on Store
    client itself only shipped in NVDA 2024.1. NVDA 2018–2023.3 have no code
    that fetches `{version}.json` at all, so no mirror of this kind can serve
    them — "back to 2018" is structurally impossible, not just a size problem.
  - GitHub Pages forbids symlinks in Actions artifacts (and dereferences them
    on upload anyway), so the mirror writes **real copies** for every locale.
    Every build reads NV Access's live `addon-datastore` metadata and publishes
    every API version from NVDA 2024.1 onward, including experimental versions.
    Existing endpoints are never pruned when a newer version appears. The
    bundled `nvdaAPIVersions.json` is the offline fallback, and also supplies
    each version's `BACK_COMPAT_TO`. Users on a version whose file is absent
    still get the `latest` (incompatible) view.
- **Version sanitization**: many non-GitHub add-ons use versions NVDA's
  `MajorMinorPatch` can't natively hold (`4.1.1009.12`, `2023.12.10.06.44.50`,
  `v20`, `1.0-beta`). The mirror keeps the first up-to-three integer runs and
  pads with `0`; `addonVersionName` keeps the original string for display.
- **Bandwidth + hashes**: the combined catalogs are large, so the first run
  downloads each unhashed package once. `hashcache.json` stores the SHA-256,
  version, size, ETag, and Last-Modified validator; changed validators or
  versions trigger a re-download, including same-size replacements.
  `githubOwnerCache.json` stores validated manifests, repository discovery, and
  conditional GitHub release ETags so unchanged ten-minute checks normally use
  quota-free HTTP 304 responses.
- **No vetting**: neither source is audited. bestmidi's disclaimer applies
  ("not tested, not an official repository"); nvda-addons.ru carries the same
  caveat. The SHA-256 hash guarantees immutability of what is downloaded, not
  safety.
