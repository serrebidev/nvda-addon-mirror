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
- The cron is hourly (`7 * * * *`). Hourly keeps the worst case inside the
  GITHUB_TOKEN budget of 1000 requests an hour and polls authors less. Do not
  promise a ten-minute refresh anywhere; hourly is the honest number.
- A build that cannot verify a repo reuses that repo's last verified
  `release_state`. Only a rate-limited repo with no verified state blocks
  publication. Never trade catalog completeness for quiet logs.

## NVDA has no fallback of any kind

`{base}/{lang}/{channel}/{apiVersion}.json` is fetched with the running NVDA's
own language and API version. A 404 is an **empty Add-on Store**, not a
degraded one: no fallback to another version, and none to English.

So dropping a locale is never an option, and retiring an API version strands
everyone still on it. `--api-version-years` exists because every release line
costs a full catalog copy per locale (~120 MB) and the site has a ceiling.

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

## Do not reformat the data files

Their diffs are reviewed. Reformatting one buries a real change in thousands of
lines, and it has happened twice.

- `nvdaAPIVersions.json` — **tabs**, matching nvaccess/addon-datastore.
- `translations.json` — `indent=1`, insertion order. Never `sort_keys`.

`translations.json` is hand-written and always wins over the generated
`autoTranslations.json`, merged per field. Never overwrite a human correction
with a model's answer.

## Checks

```
python -W error -m unittest discover -s tests   # what CI runs
python mirror.py --sources ru --limit 6 --skip-download --locales en
```

A test that hardcodes a cache key or a floor will pass for the wrong reason
after a version bump. Derive those from the module instead — see
`_owner_cache_key` and `UNMISTAKABLE_FLOOR` in the tests.

`audit_catalog()` reads its floors back from the build's own `stats.json`, so
it cannot catch a wrong floor — only an inconsistent one. Verify compatibility
changes against NVDA's source, not against the build's own output.
