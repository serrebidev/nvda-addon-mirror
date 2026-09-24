#!/usr/bin/env python3
"""Report the catalog's remaining non-English metadata for the maintainer.

``translations.json`` is hand-maintained, and the catalogs it draws from keep
growing, so it decays: every new Spanish, Russian, Turkish, French, Portuguese,
German or Chinese add-on reaches NVDA's Add-on Store untranslated until somebody
notices. ``audit_translations.py`` finds those; the maintainer closes them by
adding an English ``summary`` and ``description`` for each add-on ID to
``translations.json``, which always wins over the generated overlay below.

Two judgments the maintainer makes per item, neither decidable by pattern:

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

There used to be a machine-translation provider here (OpenRouter). It is gone:
translation is the maintainer's job now. This script reports the queue --
``collect_work()`` is what still needs English -- and rebuilds
``autoTranslations.json``, the generated overlay published with the site and
restored on the next build, from the cache of answers already given.

    python auto_translate.py --addons public/addons.json --out autoTranslations.json

An untranslated add-on keeps the text it has today, which is what it would
have had anyway.
"""

import argparse
import json
import sys

import mirror
import audit_translations



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


#: Bump when the rules below change, so every cached answer is decided again
#: under the new ones instead of the old ones being served forever.
PROMPT_VERSION = "v2-names"


def _cache_key(field, text):
    """Key a translation by what was translated, not by which add-on it was.

    The same summary reaches the mirror from several catalogs under different
    ids, and an add-on that is renamed keeps its text. Keying on the source text
    means neither costs a second call.
    """
    return f"{PROMPT_VERSION}:{field}:{mirror.translation_key(text, 'en')}"


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


def with_original_text(addons, sources):
    """Put back the text the generated overlay replaced in the built catalog.

    ``addons.json`` already carries last build's English. Judged on that, an
    add-on translated once looks finished, falls out of the next overlay, and
    is published untranslated the build after -- so the catalog flipped every
    build. Judging the original text keeps each add-on's answer in the overlay
    for as long as its text is unchanged, and only new or changed text is sent.
    """
    if not sources:
        return addons
    restored = []
    for entry in addons:
        original = sources.get(entry.get("addonId"))
        if isinstance(original, dict):
            entry = dict(entry)
            for field in ("displayName", "description"):
                if isinstance(original.get(field), str):
                    entry[field] = original[field]
        restored.append(entry)
    return restored


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


def translate(addons, cache, budget=None):
    """Report what still needs English. Returns 0: nothing is translated here.

    The machine-translation provider is retired; translation is the
    maintainer's job, worked from the queue this reports and recorded in
    ``translations.json`` (which always wins over the generated overlay).
    ``budget`` is accepted and ignored, so older invocations keep running.
    """
    work = collect_work(addons, cache)
    if not work:
        log("Nothing to translate: every non-English field already has English.")
        return 0
    log(f"{len(work)} string(s) still need English from the maintainer; "
        "add them to translations.json and the next build publishes them.")
    return 0


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
    parser.add_argument("--sources", default=mirror.AUTO_TRANSLATION_SOURCES_PATH,
                        help="pre-overlay text written by mirror.py")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be translated and stop")
    args = parser.parse_args(argv)

    addons = with_original_text(
        audit_translations.load_addons(args.addons), load_cache(args.sources))
    cache = load_cache(args.cache)

    if args.dry_run:
        work = collect_work(addons, cache)
        log(f"{len(work)} string(s) would be translated:")
        for item in list(work.values())[:40]:
            log(f"  [{item['field']}] {item['text'][:90]}")
        return 0

    translate(addons, cache)

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
