#!/usr/bin/env python3
"""Translate the catalog's remaining non-English metadata into English.

``translations.json`` is hand-maintained, and the catalogs it draws from keep
growing, so it decays: every new Spanish, Russian, Turkish, French, Portuguese,
German or Chinese add-on reaches NVDA's Add-on Store untranslated until somebody
notices. ``audit_translations.py`` finds those; this closes them.

Two jobs, both decided by a model because neither is decidable by pattern:

1. Translate a non-English ``displayName`` or ``description`` into English. A
   name is not translated away -- "Betimleyici" becomes
   "Descriptor, AKA Betimleyici", so it stays recognisable from its own
   documentation while saying what it is.

2. Rewrite a name written as "Original (English)". The overlay has 480 names
   ending in a parenthetical, but most gloss what the add-on *does*
   ("DECtalk (DECtalk speech synthesizer)") rather than translating its name,
   and turning those into "AKA" would be nonsense. Telling the two apart needs
   to know that "Betimleyici" is Turkish while "ClipboardEnhancement" is
   English, which no regex here can do.

Output is ``autoTranslations.json``, a generated overlay published with the site
and restored on the next build. ``translations.json`` always wins over it, so a
human correction is never overwritten by the model.

    python auto_translate.py --addons public/addons.json --out autoTranslations.json

Never raises on a provider failure: an untranslated add-on keeps the text it has
today, which is what it would have had anyway.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import mirror
import audit_translations

#: OpenRouter, so one key covers both this and the per-locale translation.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

#: Reasoning is mandatory on this model and cannot be turned off, but "low"
#: spends zero reasoning tokens and answers identically for translation --
#: 5.5x cheaper than "medium" in measurement, at about $0.00006 per string.
TRANSLATE_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-3.8-flash")
REASONING_EFFORT = "low"

#: Strings per request. Batching amortises the prompt across many translations,
#: which is where nearly all of the saving comes from.
BATCH_SIZE = 20

#: Ceiling per run, so a first pass over a large backlog is spread across builds
#: rather than spent in one. Roughly 3000 strings at the measured rate.
DEFAULT_BUDGET = int(os.environ.get("AUTO_TRANSLATE_BUDGET", "600"))

_SYSTEM_PROMPT = """You translate NVDA screen-reader add-on metadata into English.

Rules:
- Translate into natural English. Reply with the translation only.
- NEVER translate a product name, a brand, a key name (NVDA+F12), a file
  extension, or a URL. Leave them exactly as they are.
- For a "name" field whose value is a NON-ENGLISH NAME, answer with the English
  name, then ", AKA ", then the original name exactly as given.
  Example: "Betimleyici" -> "Descriptor, AKA Betimleyici".
- For a "name" field whose value is ALREADY ENGLISH, answer with it unchanged.
- For a "name" written as "Original (English)" where the parenthetical is an
  English rendering of a non-English NAME, answer "English, AKA Original".
  Example: "Betimleyici (Descriptor)" -> "Descriptor, AKA Betimleyici".
- For a "name" written as "Name (what it does)", where the parenthetical
  describes the add-on rather than translating its name, answer with the name
  unchanged. Example: "DECtalk (DECtalk speech synthesizer)" -> "DECtalk".
- For a "description" field, translate the prose into English and keep the
  original line breaks. Do not add commentary.

Reply with ONLY a JSON object mapping each input key to its answer string."""


def log(message):
    print(message, flush=True)


def load_cache(path):
    """Load the generated overlay, or {} for anything unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_key(field, text):
    """Key a translation by what was translated, not by which add-on it was.

    The same summary reaches the mirror from several catalogs under different
    ids, and an add-on that is renamed keeps its text. Keying on the source text
    means neither costs a second call.
    """
    return f"{field}:{mirror.translation_key(text, 'en')}"


def needs_english(entry):
    """Return {field: text} for the fields of one add-on that are not English.

    Reuses the auditor's classifier, which combines a Unicode-script test,
    function words, accented words and langdetect, and is deliberately biased
    towards precision -- a false positive here spends money rewriting text that
    was already fine.
    """
    fields = {}
    for field in ("displayName", "description"):
        text = (entry.get(field) or "").strip()
        if not text:
            continue
        if audit_translations.classify(text, short_name=field == "displayName"):
            fields[field] = text
    return fields


def parenthetical_names(entry):
    """Return {"displayName": text} when a name may be "Original (English)".

    Only a candidate: whether the parenthetical translates the name or
    describes the add-on is left to the model, which is the part a pattern
    cannot decide. Names that already carry ", AKA " are settled and skipped.
    """
    name = (entry.get("displayName") or "").strip()
    if not name or mirror.AKA_SEPARATOR in name:
        return {}
    if not (name.endswith(")") and "(" in name[:-1]):
        return {}
    return {"displayName": name}


def answer_is_usable(field, source, english):
    """Reject an answer that rewrote the half it was told to copy verbatim.

    The point of "English, AKA Original" is that the original stays
    recognisable from the add-on's own documentation. A model that tidies it --
    respacing, camel-casing, dropping a hyphen -- produces a name that matches
    nothing, so the answer is dropped and the add-on keeps the text it has.
    """
    if mirror.AKA_SEPARATOR not in english:
        return True
    original = english.split(mirror.AKA_SEPARATOR, 1)[1].strip()
    if not original:
        return False
    return original in source


def _post(payload, timeout=120):
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": mirror.USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def translate_batch(items, retries=2):
    """Translate {key: {"field":..., "text":...}}; return {key: english} or None.

    None means the whole batch failed. Nothing partial is ever returned: a
    short or reordered answer would attach one add-on's text to another.
    """
    if not items or not OPENROUTER_API_KEY:
        return None
    payload = {
        "model": TRANSLATE_MODEL,
        "reasoning": {"effort": REASONING_EFFORT},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                {key: {"field": item["field"], "value": item["text"]}
                 for key, item in items.items()},
                ensure_ascii=False,
            )},
        ],
    }
    for attempt in range(retries + 1):
        try:
            body = _post(payload)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                ValueError) as exc:
            log(f"  provider error: {exc}")
            if attempt == retries:
                return None
            time.sleep(2 * (attempt + 1))
            continue
        if body.get("error"):
            log(f"  provider error: {str(body['error'])[:200]}")
            return None
        try:
            content = body["choices"][0]["message"]["content"]
            answers = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log(f"  unreadable answer: {exc}")
            if attempt == retries:
                return None
            continue
        if not isinstance(answers, dict):
            return None
        # Keep only keys that were asked about, and only non-empty strings.
        cleaned = {
            key: value.strip()
            for key, value in answers.items()
            if key in items and isinstance(value, str) and value.strip()
        }
        cost = (body.get("usage") or {}).get("cost")
        return cleaned, cost
    return None


def collect_work(addons, cache):
    """Return {cache key: {field, text}} for everything still to translate."""
    work = {}
    seen = set()
    for entry in addons:
        addon_id = entry.get("addonId")
        if not addon_id or addon_id in seen:
            continue
        seen.add(addon_id)
        candidates = dict(needs_english(entry))
        # A parenthetical name is worth a look even when the text reads as
        # English, because "Betimleyici (Descriptor)" does.
        candidates.update(parenthetical_names(entry))
        for field, text in candidates.items():
            key = _cache_key(field, text)
            if key not in cache:
                work[key] = {"field": field, "text": text}
    return work


def translate(addons, cache, budget=DEFAULT_BUDGET):
    """Fill the cache with English for whatever still needs it. Returns count."""
    work = collect_work(addons, cache)
    if not work:
        log("Nothing to translate: every non-English field already has English.")
        return 0
    log(f"{len(work)} string(s) need English; budget is {budget} this run.")
    if not OPENROUTER_API_KEY:
        log("OPENROUTER_API_KEY is unset, so nothing was translated. "
            "Those add-ons keep the text they have today.")
        return 0

    keys = list(work)[:budget]
    done = 0
    spent = 0.0
    for start in range(0, len(keys), BATCH_SIZE):
        batch = {key: work[key] for key in keys[start:start + BATCH_SIZE]}
        result = translate_batch(batch)
        if result is None:
            log("  batch failed; stopping this run rather than retrying blindly")
            break
        answers, cost = result
        spent += cost or 0.0
        for key, english in answers.items():
            # An answer identical to the input is a decision too ("this name is
            # already English"), and caching it stops it being asked again.
            cache[key] = english
            done += 1
        log(f"  {min(start + BATCH_SIZE, len(keys))}/{len(keys)} "
            f"(${spent:.4f} so far)")
    log(f"Translated {done} string(s) for ${spent:.4f}.")
    return done


def build_overlay(addons, cache):
    """Turn the translation cache into an addonId-keyed English overlay.

    Shaped exactly like translations.json so mirror.py can merge the two, with
    the hand-maintained file winning wherever both have an opinion.
    """
    overlay = {}
    seen = set()
    for entry in addons:
        addon_id = entry.get("addonId")
        if not addon_id or addon_id in seen:
            continue
        seen.add(addon_id)
        candidates = dict(needs_english(entry))
        candidates.update(parenthetical_names(entry))
        fields = {}
        for field, text in candidates.items():
            english = cache.get(_cache_key(field, text))
            if english and not answer_is_usable(field, text, english):
                continue
            if english and english != text:
                # The store's field is displayName; the overlay's is summary.
                fields["summary" if field == "displayName" else field] = english
        if fields:
            overlay[addon_id] = fields
    return overlay


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Translate remaining non-English catalog text into English.")
    parser.add_argument("--addons", help="path to a built addons.json")
    parser.add_argument("--out", default="autoTranslations.json",
                        help="generated English overlay to write")
    parser.add_argument("--cache", default="autoTranslationCache.json",
                        help="translations already paid for, keyed by source text")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help="maximum strings to translate in this run")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be translated and stop")
    args = parser.parse_args(argv)

    addons = audit_translations.load_addons(args.addons)
    cache = load_cache(args.cache)

    if args.dry_run:
        work = collect_work(addons, cache)
        log(f"{len(work)} string(s) would be translated:")
        for item in list(work.values())[:40]:
            log(f"  [{item['field']}] {item['text'][:90]}")
        return 0

    translate(addons, cache, budget=args.budget)

    with open(args.cache, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    overlay = build_overlay(addons, cache)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"translations": overlay}, handle,
                  ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    log(f"Wrote {len(overlay)} add-on(s) to {args.out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
