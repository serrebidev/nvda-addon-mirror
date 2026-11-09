# Working on this repo

Notes that are expensive to rediscover. Everything else is in `README.md`.

## Never re-download add-ons on a schedule

An add-on author emailed on 2026-09-05: data-centre IPs were pulling every
release asset every ten minutes. The build was re-verifying every cached
download URL each run with ranged GETs, and servers that ignore ranges served
the whole file.

Authors watch their download counters and pay for the bandwidth. Bulk periodic
re-downloads look like abuse.

- Downloads are gated by `cached_download()` — a 24h recheck, a conditional GET,
  and a 6h backoff after a failure — and by `cached_pinned_bundle()`, keyed on
  asset id, `updated_at` and size. Do not reintroduce per-run verification.
- **Reading something new out of a bundle must not force a re-download.** The
  manifest version, the author's translations and the add-on's own name are all
  captured from bytes already being streamed for the SHA-256. Only a version no
  catalog states earns a forced fetch (`inspect=True`); everything else waits
  for the ordinary recheck, and coverage fills in over a day. Wiring a new
  capture into the forced path would refetch the whole catalog in one build.
- **The one exception is `manifest.ini` itself, and it is never a re-download.**
  A catalog row is only a claim: NVDA installs by what the manifest says. A
  wrong addonId or compatibility claim makes an add-on uninstallable, yet a
  release that answers 304 forever and a bundle over `MANIFEST_CAPTURE_LIMIT`
  never yield a body to read it from. So `read_manifest_by_range()` reads the zip
  directory and `manifest.ini` (2 requests, about 128 KB), once per URL.
  `read_unread_manifests()` does this in the next build rather than leave an ID
  wrong until the recheck; a failed read waits for the recheck. A 200 is closed
  unread and the refusal is remembered as `None`. Do not extend this to per-run
  checks or to locale files.
- `apply_manifest_claims()` publishes the corrected version while
  `cached_download()` keeps keying on `catalog_version`. Comparing the corrected
  one would look like a new release and re-download the bundle.
- The refresh runs every half hour from a systemd timer on the owner's
  server (`scripts/mirror-cron.sh`). That cadence keeps the worst case inside
  the GITHUB_TOKEN budget of 1000 requests an hour and polls authors less. Do
  not promise a ten-minute refresh anywhere; half-hourly is the honest number.
- A build that cannot verify a repo reuses that repo's last verified
  `release_state`. Pinned GitHub/Forgejo pins do the same through
  `_pinned_cached()` (`.bundlecache/pinnedState.json`): a 403/429/5xx or
  network fault reuses the last verified release list and fork verdict; a
  404 still fails. Only a rate-limited repo with no verified state blocks
  publication. Never trade catalog completeness for quiet logs.
- A timeout, 408, 429 or 5xx on a file already verified for this version keeps
  it published for `DOWNLOAD_OUTAGE_GRACE_SECONDS`. nvda.ru times out from some
  runners while its files are fine. A 404 or a changed version is rejected at
  once. `download-failed` in the build log counts only those rejections.

## Forgejo authors

The owner's add-ons release on Forgejo (`git.serrebiradio.com`, user
`serrebi`), not GitHub. The `forgejo_owner` source scans the explicit
repository list in `forgejoOwners.json` — no account discovery, no token
needed for public releases — and outranks `github_owner` in dedupe, so a
Forgejo release always wins over its GitHub sibling. Its entries and
release state share `githubOwnerCache.json` under the `__forgejo__` key,
which the GitHub source carries through every cache write; keep it that
way (one file, one restore path — a second cache file would need its own
plumbing in the refresh script). A `pinned.json` entry names `"host": "forgejo"`
to pin a Forgejo release. Keep `githubOwners.json` free of owner repos:
every repo the owner publishes lives in `forgejoOwners.json`.

## NVDA has no fallback of any kind

`{base}/{lang}/{channel}/{apiVersion}.json` is fetched with the running NVDA's
own language and API version. A 404 is an **empty Add-on Store**, not a
degraded one: no fallback to another version, and none to English.

So dropping a locale is never an option, and retiring an API version strands
everyone still on it. `--api-version-years` exists because every API version
costs a full catalog copy per locale (~120 MB) and the site has a ceiling.

Patches count. NV Access listed 2026.3.1 while it was still an RC; dropping
2026.3.0 as "superseded" broke the store for all stable users from 2026-11-02.
Check `{base}/en/all/{version}.json` for the version NVDA has actually shipped.

`BACK_COMPAT_TO` rises on the first release of each year. An add-on whose
`lastTestedNVDAVersion` is below it does not merely look stale — NVDA refuses
to load it. That includes this repo's own helper add-on.

## manifest.ini `summary` is the add-on's NAME

It is what NVDA shows as the name, and authors regularly write a description
there. Publishing that verbatim leaves a paragraph where a title belongs and
the real name nowhere — unusable when arrowing a list of names rather than
reading it. `best_display_name()` handles this; `looks_like_prose_name()` uses
grammatical signals and never length alone, because real names are often long.

Catalog feeds are not authoritative about channels either. nvda-addons.ru marks
89% of its links "Dev", so a pre-release label from it is honoured only when the
version string corroborates it.

## Two add-ons, one manifest name

NVDA keys add-ons by manifest `name`, so two authors' add-ons sharing one
collapse into a single store row and one hides the other. Pin the fork in
`pinned.json` as `{owner}-{name}` with `"fork_policy": "include"` and
`"optional": true`; the original keeps the name. Catalog rows whose download
URL is in a pinned repo are dropped, or they would take the shared name back.
A failed optional pin hides only that fork for one build. Do not pin a fork
over the site size budget (Supertonic 1.0.0 is 281 MB): the site is
already past the 1 GB soft limit.

## Do not reformat the data files

Their diffs are reviewed. Reformatting one buries a real change in thousands of
lines, and it has happened twice.

- `nvdaAPIVersions.json` — **tabs**, matching nvaccess/addon-datastore.
- `translations.json` — `indent=2`, insertion order. Never `sort_keys`.

`translations.json` is hand-written and always wins over the generated
`autoTranslations.json`, merged per field. Never overwrite a human correction
with a model's answer.

## Checks

```
python -W error -m unittest discover -s tests   # what the refresh runs first
python mirror.py --sources ru --limit 6 --skip-download --locales en
```

`tests/test_end_to_end.py` runs `main()` for three builds against a fake internet
and installs every published row through `tests/nvda_contract.py`, a copy of
NVDA's load and install rules that shares no parsing with `mirror.py`. A new
catalog-versus-bundle bug belongs there as a scenario. When NVDA's store code
changes, update the contract from its source.

A test that hardcodes a cache key or a floor will pass for the wrong reason
after a version bump. Derive those from the module instead — see
`_owner_cache_key` and `UNMISTAKABLE_FLOOR` in the tests.

`audit_catalog()` reads its floors back from the build's own `stats.json`, so
it cannot catch a wrong floor — only an inconsistent one. Verify compatibility
changes against NVDA's source, not against the build's own output.

## Translation is the maintainer's job (2026-09-24)

The ONLY translation job is non-English -> English. Never translate English
into other locales.

- **Into locales:** nothing. Non-English locales show the official store's own
  per-language text and author-supplied bundle translations where they exist;
  everything else stays in English. The old machine-translation tier, the
  `translationSeed.json` merge, and the `translationRequests.json` queue were
  removed 2026-09-24 (wrong direction); `translationSeed.json` was deleted.
- **Into English:** `audit_translations.py` finds add-ons publishing
  non-English text; the maintainer adds an English `summary`/`description`
  per add-on ID to `translations.json`. `auto_translate.py` only reports the
  queue now (`collect_work()`) and rebuilds `autoTranslations.json` from the
  existing cache.
- Never put a provider key back into the refresh script or the code. The old
  `OPENROUTER_API_KEY` secret was deleted.

## Muse cloud check: finish the translation queue

The Muse cloud agent is the maintainer when a gap is left open. Every
scheduled refresh writes the queue into its log, before it touches the
translation-gap issue, so the queue exists even when the API could not open
or update the issue. The refresh itself runs `scripts/muse_translate.py`
first, which fills missing fields through the owner's muse.ai agent and
commits them; the manual flow below is the fallback when that step fails.

1. Read the queue from the newest run:
   `ssh root@100.103.9.33 'grep TRANSLATION-QUEUE /var/log/nvda-addon-mirror.log | tail -40'`.
   Each line after the prefix is one JSON finding: `addonId`, `sourceURL`,
   and `fields` with the full non-English `text`.
   `TRANSLATION-QUEUE-EMPTY` means nothing to do. The open `translation-gap`
   issue shows the same list, truncated.
2. If the server is unreachable, run the auditor directly; it reads the live
   site and needs no server access:
   `python audit_translations.py --json findings.json`.
3. For each `addonId`, add an English `summary` (for `displayName`) and/or
   `description` to `translations.json` under that exact id. A name gets the
   English first and the original after `, AKA ` (`"Descriptor, AKA
   Betimleyici"`). Only add missing fields; never change a field a human
   already wrote. Keep the file's format: `indent=2`, insertion order, real
   UTF-8 characters rather than `\u` escapes, no `sort_keys`.
4. Run `python -W error -m unittest discover -s tests`, commit only
   `translations.json`, and push to main. The next scheduled run rebuilds
   the mirror, and its audit closes the issue on its own.

## Agent skills

### Issue tracker

Issues are tracked in this repo's Forgejo Issues, via the Forgejo API (`tea` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
