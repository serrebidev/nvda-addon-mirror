import io
import json
import os
import tempfile
import unittest

import audit_translations


def _entry(addon_id, display_name, description="An English description of it."):
    return {
        "addonId": addon_id,
        "displayName": display_name,
        "description": description,
        "sourceURL": f"https://github.com/example/{addon_id}",
    }


class ClassifyTests(unittest.TestCase):
    def test_cyrillic_text_is_flagged(self):
        self.assertEqual(
            audit_translations.classify(
                "Дополнение RibbonMenu для NVDA позволяет управлять лентой."
            ),
            "non-Latin script",
        )

    def test_chinese_text_is_flagged(self):
        self.assertIsNotNone(
            audit_translations.classify("广荣tts，NVDA语音合成器插件，用于语音输出。")
        )

    def test_german_description_without_accents_is_flagged(self):
        self.assertIsNotNone(
            audit_translations.classify(
                "Sagt auf Knopfdruck den Fortschritt der Progressbar an."
            )
        )

    def test_spanish_and_portuguese_descriptions_are_flagged(self):
        for text in (
            "Permite consultar de forma rápida el coste de la energía en España.",
            "Este addon encurta um link usando a api do yourls.",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(audit_translations.classify(text))

    def test_turkish_description_is_flagged(self):
        self.assertIsNotNone(
            audit_translations.classify(
                "Görüşmeler sırasında veya sesli mesaj kaydederken mikrofon "
                "sesinin uygulamalar tarafından otomatik kısılmasını engeller."
            )
        )

    def test_plain_english_description_is_not_flagged(self):
        self.assertIsNone(
            audit_translations.classify(
                "Adds quiet-mode appModules for Windows consoles to suppress "
                "noisy terminal output while you work."
            )
        )

    def test_short_proper_noun_name_is_not_flagged(self):
        # langdetect calls this Italian at p=1.0; the length floor stops it.
        self.assertIsNone(
            audit_translations.classify("RHVoice Elena, Russian voice", short_name=True)
        )

    def test_capitalized_accented_proper_nouns_are_not_flagged(self):
        # An English sentence naming a Vietnamese author must stay unflagged.
        self.assertIsNone(
            audit_translations.classify(
                "The Vnspeak reader for NVDA, created by Lê Anh Tuấn together "
                "with the Nhật Hồng Blind Support Center with sponsorship."
            )
        )

    def test_english_word_extension_is_not_a_french_signal(self):
        self.assertIsNone(
            audit_translations.classify(
                "NVDA extension for Volksverschlüsselung (public encryption)."
            )
        )

    def test_placeholder_descriptions_are_ignored(self):
        for text in ("No description", "Not found", "Unknown"):
            with self.subTest(text=text):
                self.assertIsNone(audit_translations.classify(text))

    def test_urls_do_not_supply_the_signal(self):
        # The host name alone must not make an English sentence look Spanish.
        self.assertIsNone(
            audit_translations.classify(
                "Shows the current electricity price, as published on the web "
                "at https://tarifaluzhora.es/el/para/una today."
            )
        )

    def test_short_foreign_name_is_flagged_on_one_function_word(self):
        self.assertIsNotNone(
            audit_translations.classify("TDK ve Sözlükler", short_name=True)
        )

    def test_one_stray_function_word_does_not_flag_a_description(self):
        self.assertIsNone(
            audit_translations.classify(
                "Announces the value of a progress bar on demand, so you can "
                "check on a long download without leaving the window.",
            )
        )


class AuditTests(unittest.TestCase):
    def test_non_english_name_beside_english_description_is_flagged(self):
        findings = audit_translations.audit([
            _entry("agenda", "Extension pour le programme Agenda"),
        ])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["addonId"], "agenda")
        self.assertEqual(list(findings[0]["fields"]), ["displayName"])

    def test_clean_catalog_produces_no_findings(self):
        self.assertEqual(
            audit_translations.audit([
                _entry("quietConsole", "Quiet Console"),
                _entry("baumVarioPro", "BAUM VarioPro driver"),
            ]),
            [],
        )

    def test_each_addon_id_is_reported_once_across_channels(self):
        stable = _entry("ribbonMenu", "RibbonMenu (Меню ленты)", "Дополнение для NVDA.")
        beta = dict(stable, channel="beta")
        findings = audit_translations.audit([stable, beta])
        self.assertEqual(len(findings), 1)

    def test_entries_without_an_addon_id_are_skipped(self):
        self.assertEqual(audit_translations.audit([{"displayName": "Sin id"}]), [])


class MainTests(unittest.TestCase):
    def _run(self, addons, *args):
        directory = tempfile.mkdtemp()
        addons_path = os.path.join(directory, "addons.json")
        with io.open(addons_path, "w", encoding="utf-8") as handle:
            json.dump(addons, handle)
        report_path = os.path.join(directory, "report.json")
        code = audit_translations.main(
            ["--addons", addons_path, "--json", report_path, "--quiet", *args]
        )
        with io.open(report_path, encoding="utf-8") as handle:
            return code, json.load(handle)

    def test_clean_catalog_exits_zero(self):
        code, findings = self._run([_entry("quietConsole", "Quiet Console")])
        self.assertEqual(code, 0)
        self.assertEqual(findings, [])

    def test_untranslated_catalog_exits_one_and_writes_the_report(self):
        code, findings = self._run([
            _entry("tts_converter", "TTSConverter (Конвертер)", "Дополнение для NVDA."),
        ])
        self.assertEqual(code, 1)
        self.assertEqual([f["addonId"] for f in findings], ["tts_converter"])
        self.assertIn("displayName", findings[0]["fields"])


class TranslationsFileTests(unittest.TestCase):
    """The overlay itself must stay loadable and unambiguous."""

    def _raw(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "translations.json")
        with io.open(path, encoding="utf-8", newline="") as handle:
            return handle.read()

    def test_no_duplicate_addon_ids(self):
        # A repeated key silently discards the earlier entry's fields, which is
        # how mandateOfHeavenSubtitles lost its translated summary.
        duplicates = []

        def hook(pairs):
            seen = set()
            for key, _ in pairs:
                if key in seen:
                    duplicates.append(key)
                seen.add(key)
            return dict(pairs)

        json.loads(self._raw(), object_pairs_hook=hook)
        self.assertEqual(duplicates, [])

    def test_every_entry_only_holds_known_string_fields(self):
        translations = json.loads(self._raw())["translations"]
        allowed = {"summary", "description", "author", "changelog"}
        for addon_id, entry in translations.items():
            with self.subTest(addonId=addon_id):
                self.assertTrue(set(entry) <= allowed, f"unknown field in {addon_id}")
                self.assertTrue(all(isinstance(v, str) and v for v in entry.values()))


if __name__ == "__main__":
    unittest.main()
