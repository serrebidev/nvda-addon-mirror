#!/usr/bin/env python3
"""Report add-ons the mirror still publishes in a language other than English.

``translations.json`` is a hand-maintained overlay (see ``load_translations``
and ``_translate_entry`` in ``mirror.py``). The catalogs it draws from keep
growing, so the map decays: every new Spanish, Russian, Turkish, French,
Portuguese, German or Chinese add-on reaches NVDA's Add-on Store untranslated
until somebody notices.

This auditor reads the *published output* rather than the overlay, because a
non-English field surviving in ``addons.json`` is a real gap no matter what the
overlay claims -- a key whose spelling drifted from the add-on id looks present
in ``translations.json`` and does nothing at all.

Detection is deliberately biased towards precision. A very short Latin-script
name ("Utilidades Chrome", "Le dictionnaire") carries too little signal to
separate from an English product name, so a handful of those are missed; every
description long enough to read as prose is caught.

Exit status is 0 when nothing needs translating and 1 when candidates were
found, so a scheduled job can branch on it.

    python audit_translations.py                       # audit the live mirror
    python audit_translations.py --addons public/addons.json
    python audit_translations.py --json report.json --quiet
"""

import argparse
import json
import re
import sys
import urllib.request

import mirror

DEFAULT_ADDONS_URL = "https://serrebidev.github.io/nvda-addon-mirror/addons.json"

#: Fields carrying prose a user reads in the store. ``displayName`` and
#: ``description`` are checked separately on purpose: several real gaps had a
#: non-English name beside an English description, and a combined check --
#: which the longer description dominates -- missed every one of them.
CHECKED_FIELDS = ("displayName", "description")

#: Text shorter than this is mostly proper nouns and abbreviations, where every
#: signal available here misfires ("RHVoice Elena, Russian voice" is English but
#: reads as Italian to a statistical detector).
MIN_LENGTH = 15

#: A name this short is a product name, not prose, so one function word is
#: enough to act on. Descriptions need corroboration.
SHORT_NAME_LENGTH = 40

#: Placeholders the pipeline substitutes for absent upstream metadata. Already
#: English, and carrying nothing to translate.
PLACEHOLDERS = frozenset({"no description", "not found", "unknown", "n/a"})

#: langdetect is only consulted on text long enough for it to be trustworthy.
#: Below this it reports high confidence on noise -- it calls the ASCII string
#: "a11y-task-recorder" Spanish at p=1.0.
MIN_DETECTOR_LENGTH = 60

#: Function words frequent in one European language and effectively absent from
#: English prose. Follows the spirit of
#: ``mirror._NON_ENGLISH_LATIN_CHANGELOG_RE`` but covers running text rather
#: than changelog headings. Words that are also ordinary English -- "a", "con",
#: "die", "per", "son", "no", "man", "dan", "sono" -- are deliberately absent,
#: and so is "addon", which English add-on blurbs are full of. Accented
#: spellings are written as they actually occur; the text is not folded.
_LATIN_FUNCTION_WORDS = (
    # Spanish
    "el|los|las|una|unos|unas|del|para|por|pero|desde|como|cuando|sobre|"
    "permite|complemento|usuario|usuarios|puede|puedes|este|esta|estos|"
    "rápida|información|utilizando|disponible|utilidades|gestor|"
    # Portuguese
    "um|uma|você|seu|sua|usando|encurta|através|também|"
    "onde|meus|minhas|não|"
    # French
    "le|les|des|une|pour|avec|dans|vous|votre|cette|cet|ces|permet|lorsque|"
    "leur|offre|meilleure|logiciel|rendre|dictionnaire|"
    # German
    "und|ein|eine|einen|mit|auf|den|dem|nicht|der|das|oder|wird|"
    "werden|beim|durch|kann|auch|sagt|"
    # Italian
    "gli|della|questo|questa|quando|anche|viene|essere|nella|dalla|"
    # Turkish
    "ve|bir|için|ile|veya|olarak|seçili|kullanıcı|"
    "sırasında|tarafından|otomatik|engeller|ayarları|"
    # Indonesian
    "dengan|untuk|yang|adalah|pada|dari|akan|bisa|dapat|"
    # Vietnamese
    "của|dùng|không|người|tiếng|"
    "cộng|đồng|trực|tiếp"
)

_LATIN_HINT_RE = re.compile("(?i)\\b(?:" + _LATIN_FUNCTION_WORDS + ")\\b")

#: An accented word that is *not* capitalized. English descriptions are full of
#: capitalized accented proper nouns -- voice names like "Letícia-Plus", authors
#: like "Lê Anh Tuấn" -- and counting bare letters flagged all of them. A
#: lowercase accented word is running prose in another language instead.
_LOWERCASE_ACCENTED_WORD_RE = re.compile(
    r"(?:^|(?<=[^\w'-]))[a-zß-ÿā-ɏḀ-ỿ]*"
    r"[ß-ÿā-ɏḀ-ỿ]"
    r"[a-zß-ÿā-ɏḀ-ỿ]*"
)

#: Words carrying non-English letters that are ordinary English usage, or proper
#: nouns the store is right to leave exactly as they are.
_ACCENTED_ALLOWED = re.compile(
    "(?i)\\b(?:café|résumé|naïve|exposé|cliché|"
    "façade|déjà|élite|première|fiancée?|"
    "protypé)\\b"
)

_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\S+@\S+\.\S+")

try:  # Optional: sharpens the audit, never required to run it.
    from langdetect import DetectorFactory, detect_langs

    DetectorFactory.seed = 0
except ImportError:  # pragma: no cover - only taken where the dep is absent
    detect_langs = None


def classify(text, short_name=False):
    """Return why ``text`` looks non-English, or ``None`` when it looks fine.

    Four signals are combined, because no single one was complete in testing:
    a Unicode-script test finds Cyrillic and CJK; function words find Romance
    and Germanic prose; non-English letters find accented text whose function
    words this list happens to miss; and langdetect catches the remainder.

    The middle two must corroborate each other before a *description* is
    flagged -- one stray match in a long English paragraph means nothing. A
    short ``displayName`` is held to the looser bar set by ``short_name``,
    since a product name has no room for a stray match.
    """
    text = (text or "").strip()
    if len(text) < MIN_LENGTH or text.casefold() in PLACEHOLDERS:
        return None

    if mirror._NON_LATIN_SCRIPT_RE.search(text):
        return "non-Latin script"

    # URLs and mail addresses carry foreign-looking letter runs of their own.
    stripped = _URL_RE.sub(" ", text)

    allowed = _ACCENTED_ALLOWED.sub(" ", stripped)
    words = {match.casefold() for match in _LATIN_HINT_RE.findall(stripped)}
    accented = {
        match.casefold() for match in _LOWERCASE_ACCENTED_WORD_RE.findall(allowed)
    }

    threshold = 1 if short_name and len(text) <= SHORT_NAME_LENGTH else 2
    if len(words) >= threshold:
        return "non-English function words: " + ", ".join(sorted(words)[:6])
    if words and accented:
        return "non-English function words and accented words"
    if len(accented) >= 3:
        return "accented words: " + ", ".join(sorted(accented)[:6])

    if detect_langs is not None and len(stripped) >= MIN_DETECTOR_LENGTH:
        try:
            langs = detect_langs(stripped)
        except Exception:  # langdetect raises on text with no usable features
            return None
        english = next((lang.prob for lang in langs if lang.lang == "en"), 0.0)
        if langs[0].lang != "en" and langs[0].prob >= 0.99 and english < 0.02:
            return f"detected as {langs[0].lang}"

    return None


def audit(addons):
    """Return one record per add-on id that still publishes non-English text."""
    findings = []
    seen = set()
    for entry in addons:
        addon_id = entry.get("addonId")
        if not addon_id or addon_id in seen:
            continue
        seen.add(addon_id)

        fields = {}
        for field in CHECKED_FIELDS:
            reason = classify(entry.get(field), short_name=field == "displayName")
            if reason:
                fields[field] = {"reason": reason, "text": entry.get(field)}
        if fields:
            findings.append({
                "addonId": addon_id,
                "sourceURL": entry.get("sourceURL") or entry.get("homepage") or "",
                "fields": fields,
            })
    findings.sort(key=lambda finding: finding["addonId"].casefold())
    return findings


def load_addons(path=None, url=DEFAULT_ADDONS_URL):
    """Read the published catalog from disk, or fetch it from the mirror."""
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    request = urllib.request.Request(url, headers={"User-Agent": mirror.USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def format_report(findings):
    """Render findings for a terminal, one add-on per block."""
    if not findings:
        return "No untranslated add-ons found."
    lines = [f"{len(findings)} add-on(s) still publishing non-English text:", ""]
    for finding in findings:
        source = finding["sourceURL"] or "no source URL"
        lines.append(f"{finding['addonId']}  ({source})")
        for field, detail in finding["fields"].items():
            text = " ".join((detail["text"] or "").split())
            if len(text) > 300:
                text = text[:297] + "..."
            lines.append(f"  {field} [{detail['reason']}]: {text}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report add-ons the mirror still publishes in a language "
                    "other than English."
    )
    parser.add_argument(
        "--addons", help="path to a built addons.json (default: fetch from --url)"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_ADDONS_URL,
        help=f"published catalog to audit (default: {DEFAULT_ADDONS_URL})",
    )
    parser.add_argument("--json", dest="json_path", help="write findings to this file")
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the human-readable report"
    )
    args = parser.parse_args(argv)

    findings = audit(load_addons(args.addons, args.url))

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(findings, handle, ensure_ascii=False, indent=1)

    if not args.quiet:
        print(format_report(findings))

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
