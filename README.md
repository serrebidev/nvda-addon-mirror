# NVDA Add-on Update Mirror

A self-updating mirror of two NVDA add-on catalogs, published in the exact wire
format NVDA's built-in Add-on Store consumes. It refreshes every 12 hours via
GitHub Actions and is served from GitHub Pages.

Sources:

- **[bestmidi.com/addons/](https://bestmidi.com/addons/)** — the GitHub-discovered
  "bleeding edge" list (`addons.json`).
- **[nvda-addons.ru](https://nvda-addons.ru/)** — the Russian community catalog
  (`get.php?addonslist`, the same JSON its own TiendaNVDA/Store add-ons use),
  which hosts many add-ons that never publish GitHub releases (synthesizers,
  voice packs, localized forks, etc.).

## What it does

1. Fetches both catalogs.
2. Rejects candidates that cannot be safely installed through NVDA's store:
   - no download URL,
   - missing / template add-on id,
   - a version string with no parseable numeric parts.
3. Merges the two sources by add-on id (the nvda-addons.ru entry wins on
   collision, since it's a curated catalog with direct download links).
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

Point NVDA's Add-on Store at the site:

```
https://<owner>.github.io/<repo>/
```

Two ways to do this:

1. **Install the helper add-on** (`dist/addonStoreMirror-1.0.0.nvda-addon`).
   It sets `[addonStore] baseServerURL` to the mirror on startup and restores it
   when disabled — the same mechanism
   [nvdacn/NVDAUpdateMirror](https://github.com/nvdacn/NVDAUpdateMirror) uses.
2. **Edit `nvda.ini`** manually:
   ```ini
   [addonStore]
   baseServerURL = https://<owner>.github.io/<repo>/
   ```
   then restart NVDA.

## Repo layout

- `mirror.py` — the whole pipeline (stdlib only, Python 3.11+).
- `.github/workflows/update.yml` — cron `0 */12 * * *` + manual dispatch.
- `helper/` — source of the `addonStoreMirror` helper add-on; `build_helper.py`
  packs it into `dist/`.
- `public/` — generated site (published to GitHub Pages by Actions).

## Running locally

```sh
python mirror.py --out public              # full build (both sources)
python mirror.py --sources ru --limit 6 --skip-download --locales en --copies   # fast smoke test
```

On Linux the mirror emits symlinks (a few MB of git). Use `--copies` to write
real files instead (much larger, ~1 MB per file), for local inspection on
Windows where symlink creation needs privileges.

## Notes and trade-offs

- **File layout**: NVDA requests `{base}/{lang}/{channel}/{apiVersion}.json`,
  using the language/channel/apiVersion only as cache keys — the returned list
  is identical for all of them. The mirror stores one canonical `addons.json`
  and symlinks every other path to it, so the published site is tiny. GitHub
  Pages supports this for Actions-based builds.
- **API versions**: the current NVDA version is fetched at build time and added
  to `CURATED_API_VERSIONS`. Add a version there if you need to cover an older
  NVDA release's "compatible" view.
- **Version sanitization**: many non-GitHub add-ons use versions NVDA's
  `MajorMinorPatch` can't natively hold (`4.1.1009.12`, `2023.12.10.06.44.50`,
  `v20`, `1.0-beta`). The mirror keeps the first up-to-three integer runs and
  pads with `0`; `addonVersionName` keeps the original string for display.
- **Bandwidth + hashes**: the combined catalogs are large (many voice packs are
  hundreds of MiB), so the first run downloads everything once. A `hashcache.json`
  is deployed with the site and restored at the start of each run; thereafter
  only add-ons whose download size changed are re-downloaded (checked via a
  cheap `HEAD`). This assumes same-size release assets are immutable, which
  holds for GitHub release and `nvda.ru` hosted files in practice.
- **No vetting**: neither source is audited. bestmidi's disclaimer applies
  ("not tested, not an official repository"); nvda-addons.ru carries the same
  caveat. The SHA-256 hash guarantees immutability of what is downloaded, not
  safety.
