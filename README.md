# NVDA Add-on Update Mirror

A self-updating mirror of NVDA add-on catalogs, direct author releases, and
explicitly pinned GitHub releases, published in the exact wire format NVDA's built-in Add-on Store
consumes. It refreshes hourly via GitHub Actions and is served from
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
  every hourly run; a lightweight daily account scan discovers new repos.
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

- **Add-ons published from an author's own website** — some authors distribute
  a `.nvda-addon` from their own site and never appear in a catalog or on
  GitHub, so nothing else in the build can reach them. A `pinned.json` entry
  naming a `url` instead of a `repo` publishes one. The bundle is downloaded,
  validated, and its `manifest.ini` read for version, summary, author and NVDA
  version range exactly as a GitHub pin's is. Re-downloads are avoided by the
  host's own ETag / Last-Modified / length, so a build only refetches when the
  author actually replaces the file. Such an entry is repackaged and rehosted
  **only** when it renames the add-on; an entry whose `addon_id` matches the
  bundle's own manifest name keeps pointing at the author's URL, so their
  download count still sees real installs.

  ```json
  { "url": "https://example.org/myAddon-1.2.nvda-addon", "addon_id": "myAddon" }
  ```

  `summary`, `publisher`, `channel`, `homepage`, `source_url`, `license`,
  `license_url`, `changelog`, `min_nvda_version` and `last_tested_nvda_version`
  are optional and override what the bundle states.

**Questions, bugs, or release news?** Join the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects), the fastest place to get help.

## What it does

1. Fetches every catalog (using nvda-addons.org only if nvda.es fails).
2. Rejects candidates that cannot be safely installed through NVDA's store:
   - no download URL,
   - missing / template add-on id,
   - voice and speech/data packs from the Russian catalog.
   Every source otherwise retains its entries, including free-form version
   strings and any upstream scan metadata. A catalog that states no usable
   version is not trusted over the add-on itself. That covers both a
   free-form string ("unknown", "current") and a degenerate number — nvda.ru
   lists CodeFactoryOnlineTTS as version "0" while its own file is named
   `CodeFactoryOnlineTTS-V.1.1.nvda-addon` — because either one leaves NVDA
   comparing against 0.0.0 and never seeing an update. The real version is
   taken from the download's file name first, which is free, and otherwise
   from `manifest.ini` inside the bundle the next step downloads anyway, so
   no extra request is made and the answer is cached with the hash. A
   recovered version is used only when it is genuinely the higher release.
   Only when nothing states anything numeric is the version represented as
   `0.0.0` in NVDA's required numeric comparison field, with the original
   version text preserved for display.
3. Merges sources case-insensitively by add-on id and channel. Explicitly pinned
   releases win, followed by direct author releases, the official store,
   nvda-addons.ru, bestmidi, and Spanish-catalog originals. A dev or beta entry
   whose version is the release the stable channel already carries is then
   dropped, so one add-on is listed once. nvda-addons.ru labels most of its
   links "Dev" regardless of what upstream published, which otherwise put the
   same version of ~370 add-ons in the store twice. Versions are compared as
   the numbers NVDA itself compares, so `2026.05.03` and `2026.5.3` count as
   one release.
4. Downloads each remaining `.nvda-addon`, computes its SHA-256 (NVDA enforces
   this checksum on install), and emits the NVDA store schema.
5. Translates each locale's catalog: the official store's own per-language
   view first, then the add-on's bundled `locale/<lang>/manifest.ini`, then
   machine translation for whatever is still English.
6. Writes `cacheHash.json`, `addons.json`, and
   `{lang}/{channel}/{apiVersion}.json` for every NVDA locale, channel, and a
   curated set of recent API versions.
7. Publishes everything to GitHub Pages.

> These add-ons are **untested**. bestmidi's disclaimer: *"These add-ons have
> not been tested and this is not an official NVDA add-on repository."* The
> SHA-256 hash still guarantees immutability of what you download, but nothing
> here is audited.

## Using the mirror

The mirror serves **metadata only** — it does not re-host the `.nvda-addon`
files. Each entry's `URL` points at the original host (GitHub release or
`nvda.ru` upload), and NVDA downloads directly from there. The build downloads
uncached files to compute the SHA-256 checksum NVDA enforces on install, then
reuses that result between daily checks. The helper does not crawl packages;
NVDA downloads an add-on when the user installs or updates it.

Point NVDA's Add-on Store at the live mirror:

```
https://serrebidev.github.io/nvda-addon-mirror
```

Three ways to do this:

1. **Install the helper add-on** — latest build:
   [dist/addonStoreMirror-1.3.0.nvda-addon](dist/addonStoreMirror-1.3.0.nvda-addon)
   (raw link: https://raw.githubusercontent.com/serrebidev/nvda-addon-mirror/main/dist/addonStoreMirror-1.3.0.nvda-addon).
   It sets `[addonStore] baseServerURL` to the mirror on startup and restores it
   when disabled — the same mechanism
   [nvdacn/NVDAUpdateMirror](https://github.com/nvdacn/NVDAUpdateMirror) uses.
   Version 1.3.0 retains the NVDA 2027.1 compatibility floor. Version 1.2.1
   corrected the minimum NVDA version to 2025.1 (see
   below) and stopped a failure there from leaving the Add-on Store list
   modified. 1.2.0 added source visibility and source-aware search; 1.1.1
   fixed a crash caused by replacing NVDA's live Add-on Store data manager;
   1.0.0 had a trailing-slash URL bug.
2. **Edit `nvda.ini`** manually:
   ```ini
   [addonStore]
   baseServerURL = https://serrebidev.github.io/nvda-addon-mirror
   ```
   then restart NVDA.
3. **Use NVDA's built-in mirror setting** — NVDA menu > Preferences > Settings >
   Add-on Store > Mirror server > Change..., paste
   `https://serrebidev.github.io/nvda-addon-mirror`, then OK. Available since
   NVDA 2025.1; it writes the same `[addonStore] baseServerURL` key as the two
   options above, so it works exactly as well as they do — it just won't
   restore the official URL for you when you want to stop using the mirror,
   and it doesn't add the helper add-on's Source column / source-aware search.

## Browsing what was rejected

The site publishes [rejected.html](https://serrebidev.github.io/nvda-addon-mirror/rejected.html)
— every candidate excluded while building the mirror, grouped by reason
(voice/data packs skipped, no download URL, …), with an
in-page filter. The same data is available as JSON at `rejected.json`.

## Repo layout

- `mirror.py` — the whole pipeline (stdlib only, Python 3.11+).
- `audit_translations.py` — reports add-ons still published in a language
  other than English (see below).
- `.github/workflows/update.yml` — hourly cron, with persistent build caches.
  GitHub may delay or drop scheduled events. Each build audits its own catalog
  for untranslated add-ons and manages the `translation-gap` issue (see below).
- `helper/` — source of the `addonStoreMirror` helper add-on; `build_helper.py`
  packs it into `dist/`.
- `public/` — generated site (published to GitHub Pages by Actions).

## Add-on names

`manifest.ini`'s `summary` is the field NVDA shows as an add-on's **name**, and
some authors write a description there instead. Published verbatim that leaves
a paragraph where a title belongs and the real name nowhere at all — unusable
when you are arrowing a list of names rather than reading it.

`looks_like_prose_name()` flags a display name that reads as a sentence, using
grammatical signals (a relative clause, second-person address, a description
opener, a sentence-ending period) rather than length alone — real names are
often long, like "Acapela TTS Voices for NVDA Engines" or "BOA: Better Office
Accessibility", and must not be touched.

When one is flagged, `best_display_name()` takes the first usable name from:
the catalog's own summary, the add-on's `manifest.ini` summary (read from bytes
already streamed for the SHA-256, so it costs no extra request), the name a
"Name: tagline" summary opens with, then the add-on id made readable. It never
gives up and republishes the sentence: a reconstructed title still beats a
paragraph in a list of names.

This corrected 65 names, including 8 in `translations.json` where the English
overlay had a description in its `summary` field.

### Names that are not in English

An add-on called "Gestor de BIOS y UEFI Accesible" tells an English-speaking
user nothing about what it does, but dropping its own name would stop them
recognising it from its documentation or from anywhere it is discussed. Both
are published, English first:

    Accessible BIOS and UEFI Manager, AKA Gestor de BIOS y UEFI Accesible
    System Monitor, AKA Monitor del Sistema

The English half comes from `translations.json`; the original is the add-on's
own `manifest.ini` summary. `looks_non_english_name()` decides whether there is
a pair to show at all, and errs towards no — a false positive appends a
redundant "AKA" to a name that never needed one. Nothing is appended when the
original is the same name plus a subtitle in its own language ("SoundTub" and
"SoundTub - download acessível de áudio e vídeo" are one name), nor when no
English name has been supplied yet.

## Translations

Every locale used to be a byte-identical copy of the English catalog. Each one
is now translated, best source first:

1. **The official store's own per-language view.** NV Access publishes
   `{lang}/all/latest.json` with author-supplied translations — 208 French,
   176 German and 57 Japanese descriptions differ from the English. Fetched
   with an `ETag`, so an unchanged language costs a 304 rather than 1.7 MB.
   Matching is keyed by `(addonId, channel)`, because the store lists one row
   per channel and keying on the id alone lets a dev row take a stable row's
   wording.
2. **The add-on's own `locale/<lang>/manifest.ini`.** This is where NVDA's
   `addonHandler` reads a translated summary and description, so it is the
   author's own text. Read out of bytes already being streamed for the SHA-256,
   which costs no extra request. Coverage fills in over a day as the ordinary
   recheck TTLs expire — harvesting never forces a re-download.
3. **Machine translation**, for whatever is still English. Off unless
   `OPENROUTER_API_KEY` is set; without it those entries stay English, which is
   what they were before. `TRANSLATE_CHAR_BUDGET` (default 200 000 characters
   per build) caps the spend, so the first pass is spread across builds rather
   than paid for in one run. Results are cached by `(source text, language)` in
   `translationCache.json`, so unchanged text is never paid for twice. Every
   NVDA locale can be targeted, not a provider's supported subset.

Anything with no translation at any tier keeps its English string — a gap is
never worse than the previous behaviour. `--no-translate` publishes English
everywhere and skips the per-language fetches.

Locale fallback follows NVDA's own: `pt_BR` uses a `pt` translation when there
is no `pt_BR` one.

## Translating into English automatically

`translations.json` is hand-maintained and decays: every new Spanish, Russian,
Turkish, French, Portuguese, German or Chinese add-on reaches the store
untranslated until somebody notices. `audit_translations.py` finds those;
`auto_translate.py` closes them.

It runs after each build, sends what is still not English to
`google/gemini-3.8-flash` via OpenRouter, and writes `autoTranslations.json` -
a generated overlay published with the site and restored on the next build.
`translations.json` is merged **over** it per field, so a hand-written
correction is never overwritten by the model, and correcting only a summary
does not discard a generated description.

It also settles a question no pattern here can: the overlay has 480 names
ending in a parenthetical, and they are not all the same thing.
`Betimleyici (Descriptor)` glosses a Turkish *name* and becomes
`Descriptor, AKA Betimleyici`; `DECtalk (DECtalk speech synthesizer)` describes
what the add-on *does* and becomes just `DECtalk`. Telling those apart needs to
know that "Betimleyici" is Turkish while "ClipboardEnhancement" is English.

Guards, because the model is not trusted blindly:

- An answer whose `AKA` half is not verbatim in the input is discarded. A
  tidied original (respaced, camel-cased, a hyphen dropped) matches nothing the
  user could have seen elsewhere, so the add-on keeps the text it has.
- A batch that comes back short or reordered is discarded whole, never
  partially applied - one add-on's text must never land on another's name.
- A provider failure, a missing key or an exhausted budget all leave the
  catalog exactly as it was. It never blocks a deployment.

Reasoning effort is `low`: it spends zero reasoning tokens and answers
identically for translation, measured 5.5x cheaper than `medium`. Batched, the
whole 487-string backlog cost $0.10.

## Keeping the English overlay complete

Many add-ons ship only Spanish, Russian, Turkish, French, Portuguese, German or
Chinese metadata. `translations.json` overlays English `summary`, `description`,
`author` and `changelog` text onto them, keyed by add-on id. The catalogs keep
growing, so the overlay decays unless somebody notices the new arrivals.

`audit_translations.py` finds them. It reads the **published** `addons.json`
rather than the overlay, because a non-English field that survives into the
output is a real gap no matter what the overlay claims — a key whose spelling
drifted from the add-on id looks present and does nothing:

```sh
python audit_translations.py                        # audit the live mirror
python audit_translations.py --addons public/addons.json --json gaps.json
```

It exits `0` when nothing needs translating and `1` when it found candidates,
so a scheduled job can branch on it. Detection combines a Unicode-script test
(reusing `mirror`'s own), per-language function words, lowercase accented words,
and — when `langdetect` happens to be installed — a statistical check on longer
text. It is biased towards precision: a very short Latin-script product name
carries too little signal to separate from English, so a few of those are
missed, but every description long enough to read as prose is caught.

The hourly update workflow runs the auditor against the freshly built catalog
and manages the `translation-gap` issue (`translation_issue.py sync` creates
the issue on new findings, edits it only when its content changed, and closes
it once the audit passes). New untranslated add-ons therefore surface within
an hour of their first build, and translating them in `translations.json`
closes the issue on the next hourly run — no manual issue management. The
audit is report-only: it never blocks the mirror's deployment.

## Running locally

```sh
python mirror.py --out public              # full build (all sources)
python mirror.py --sources ru --limit 6 --skip-download --locales en   # fast smoke test
python mirror.py --sources official --locales en,fr,ja --skip-download  # check translations
python mirror.py --no-translate                                       # English everywhere
```

The mirror writes real files rather than symlinks because GitHub Pages rejects
artifacts that contain symlinks.

## Notes and trade-offs

- **File layout**: NVDA requests `{base}/{lang}/{channel}/{apiVersion}.json`.
  The `apiVersion` is the *running NVDA's own* add-on API version (e.g.
  `2026.2.0`), so the mirror must emit a file for every released NVDA version
  still in use or those users get a 404 and an empty "compatible" list — NVDA
  has no fallback to another version or to English.
  - **Old NVDA support has a hard floor of NVDA 2025.1.** The Add-on Store
    client shipped earlier, in NVDA 2023.2, and 2023.2–2024.4 do fetch
    `{lang}/{channel}/{version}.json` — but from a hardcoded address:
    `addonStore.network.BASE_URL = "https://nvaccess.org/addonStore"`, with no
    setting to change it. The `[addonStore] baseServerURL` key this mirror
    depends on was added in **2025.1**, where `_getBaseURL()` first consults it.
    So no mirror of this kind can serve any NVDA older than 2025.1, and NVDA
    2018–2022 has no Add-on Store at all. The mirror publishes no endpoint
    below `2025.1.0`: files for older API versions could never be requested by
    any NVDA ever released, and they were about a third of the deployed site.
  - GitHub Pages forbids symlinks in Actions artifacts (and dereferences them
    on upload anyway), so the mirror writes **real copies** for every locale.
    Every build reads NV Access's live `addon-datastore` metadata; the bundled
    `nvdaAPIVersions.json` is the offline fallback and also supplies each
    version's `BACK_COMPAT_TO`.
  - **Which API versions are published.** Each release line costs one filtered
    copy of the catalog per locale — around 120 MB across all locales — and
    NVDA ships roughly three lines a year, so publishing every line since
    2025.1 outgrew GitHub Pages' 1 GB soft limit (the site reached 1.79 GB).
    `--api-version-years` (default 2) keeps every line from the current and
    previous NVDA years, plus the newest line of each older year. Superseded
    patches within a line are always dropped: NVDA moves those users onto the
    newest patch itself, and every patch in a line shares one `BACK_COMPAT_TO`.
    Users on a retired line still get the `latest` (incompatible) view. The
    build logs every version it stops serving and warns if the site passes
    1 GB.
- **Version sanitization**: many non-GitHub add-ons use versions NVDA's
  `MajorMinorPatch` can't natively hold (`4.1.1009.12`, `2023.12.10.06.44.50`,
  `v20`, `1.0-beta`). The mirror keeps the first up-to-three integer runs and
  pads with `0`; `addonVersionName` keeps the original string for display.
- **Bandwidth + hashes**: the combined catalogs are large, so the first run
  downloads each unhashed package once. `hashcache.json` stores the SHA-256,
  version, size, HTTP validators, and the next permitted check time. Unchanged
  versions make no package requests for 24 hours, then use a conditional GET.
  HTTP 304 keeps the existing hash; a full response is hashed once. Hosts without
  validators may require one download per day. New catalog versions bypass the
  daily interval; same-version replacements are detected at the next daily check.
  Failed downloads wait six hours before retrying. Existing legacy hashes seed
  the daily interval without a bulk download during migration. `--no-head-check`
  explicitly forces downloads and bypasses these protections.
  GitHub Actions saves build caches even after build/publication failures. On a
  cache miss, it requires a valid cache from the last deployment before building.
  Pinned bundles are cached by release asset identity and update time, so
  repackaging also avoids downloading unchanged assets on each run.
  `githubOwnerCache.json` stores validated manifests, repository discovery, and
  conditional GitHub release ETags so unchanged hourly checks normally use
  quota-free HTTP 304 responses. A repository that still hits the API rate
  limit reuses its last verified release state until the next run re-checks it;
  only a rate-limited repository with no verified state yet blocks publication.
- **No vetting**: neither source is audited. bestmidi's disclaimer applies
  ("not tested, not an official repository"); nvda-addons.ru carries the same
  caveat. The SHA-256 hash guarantees immutability of what is downloaded, not
  safety.
