"""The generated English overlay must never make the catalog worse.

Everything here guards one of three ways that could happen: rewriting the half
of a name that has to stay verbatim, overwriting a human correction, or
attaching one add-on's text to another add-on's name.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import auto_translate
import mirror


def addon(addon_id, name, description="A clock."):
    return {"addonId": addon_id, "displayName": name, "description": description}


class AnswerValidationTests(unittest.TestCase):
    def test_a_verbatim_original_is_accepted(self):
        self.assertTrue(auto_translate.answer_is_usable(
            "displayName", "Betimleyici (Descriptor)",
            "Descriptor, AKA Betimleyici",
        ))

    def test_a_tidied_original_is_rejected(self):
        # "Texto para telegram" respaced into "TextoParaTelegram" matches
        # nothing the user could have seen anywhere else.
        self.assertFalse(auto_translate.answer_is_usable(
            "displayName", "Texto para telegram",
            "Text for Telegram, AKA TextoParaTelegram",
        ))

    def test_an_empty_original_is_rejected(self):
        self.assertFalse(auto_translate.answer_is_usable(
            "displayName", "Betimleyici", "Descriptor, AKA ",
        ))

    def test_an_answer_without_aka_is_not_second_guessed(self):
        self.assertTrue(auto_translate.answer_is_usable(
            "description", "Un texto en espanol", "A text in Spanish",
        ))


class CandidateSelectionTests(unittest.TestCase):
    def test_a_parenthetical_name_is_a_candidate(self):
        self.assertEqual(
            {"displayName": "Betimleyici (Descriptor)"},
            auto_translate.parenthetical_names(
                addon("betimleyici", "Betimleyici (Descriptor)")),
        )

    def test_a_name_already_carrying_aka_is_settled(self):
        self.assertEqual(
            {},
            auto_translate.parenthetical_names(
                addon("x", "Descriptor, AKA Betimleyici")),
        )

    def test_a_plain_name_is_not_a_candidate(self):
        self.assertEqual(
            {}, auto_translate.parenthetical_names(addon("x", "Cursor Locator")))

    def test_english_text_is_not_sent_for_translation(self):
        self.assertEqual(
            {},
            auto_translate.needs_english(
                addon("x", "Cursor Locator",
                      "Reports the caret position in the current line.")),
        )

    def test_non_english_prose_is_sent(self):
        fields = auto_translate.needs_english(
            addon("x", "Cursor Locator",
                  "Complemento para descargar audio y video accesible con NVDA."))
        self.assertIn("description", fields)

    def test_work_is_keyed_by_source_text_not_by_addon(self):
        # The same summary reaches the mirror from several catalogs under
        # different ids; it must only ever be paid for once.
        entries = [
            addon("a", "Monitor del Sistema", "Monitor del Sistema de NVDA."),
            addon("b", "Monitor del Sistema", "Monitor del Sistema de NVDA."),
        ]
        work = auto_translate.collect_work(entries, {})
        self.assertEqual(len(work), len(set(work)))
        self.assertLessEqual(len(work), 2)

    def test_cached_work_is_not_collected_again(self):
        entries = [addon("a", "Monitor del Sistema")]
        first = auto_translate.collect_work(entries, {})
        cache = {key: "System Monitor" for key in first}
        self.assertEqual({}, auto_translate.collect_work(entries, cache))


class OverlayTests(unittest.TestCase):
    def test_an_unchanged_answer_produces_no_overlay_entry(self):
        # "DECtalk (DECtalk speech synthesizer)" -> "DECtalk" is a change;
        # "Cursor Locator" -> "Cursor Locator" is not, and must not be written.
        entries = [addon("x", "Cursor Locator", "Reports the caret position.")]
        cache = {
            auto_translate._cache_key("displayName", "Cursor Locator"):
                "Cursor Locator",
        }
        self.assertEqual({}, auto_translate.build_overlay(entries, cache))

    def test_a_translated_name_lands_under_summary(self):
        # The store's field is displayName; the overlay's is summary.
        entries = [addon("betimleyici", "Betimleyici (Descriptor)")]
        cache = {
            auto_translate._cache_key(
                "displayName", "Betimleyici (Descriptor)"):
                "Descriptor, AKA Betimleyici",
        }
        self.assertEqual(
            {"betimleyici": {"summary": "Descriptor, AKA Betimleyici"}},
            auto_translate.build_overlay(entries, cache),
        )

    def test_an_answer_that_mangled_the_original_is_not_published(self):
        entries = [addon("x", "Texto para telegram")]
        cache = {
            auto_translate._cache_key("displayName", "Texto para telegram"):
                "Text for Telegram, AKA TextoParaTelegram",
        }
        self.assertEqual({}, auto_translate.build_overlay(entries, cache))


class ProviderFailureTests(unittest.TestCase):
    def test_no_key_translates_nothing_and_does_not_raise(self):
        entries = [addon("x", "Monitor del Sistema")]
        cache = {}
        with mock.patch.object(auto_translate, "OPENROUTER_API_KEY", ""):
            self.assertEqual(0, auto_translate.translate(entries, cache))
        self.assertEqual({}, cache)

    def test_a_failed_batch_stops_the_run_without_writing_anything(self):
        entries = [addon("x", "Monitor del Sistema")]
        cache = {}
        with mock.patch.object(auto_translate, "OPENROUTER_API_KEY", "key"), \
                mock.patch.object(
                    auto_translate, "translate_batch", return_value=None):
            self.assertEqual(0, auto_translate.translate(entries, cache))
        self.assertEqual({}, cache)

    def test_the_budget_caps_a_single_run(self):
        entries = [addon(str(i), f"Monitor del Sistema {i}") for i in range(60)]
        cache = {}
        sent = []

        def fake(batch, retries=2):
            sent.extend(batch)
            return {key: "System Monitor" for key in batch}, 0.001

        with mock.patch.object(auto_translate, "OPENROUTER_API_KEY", "key"), \
                mock.patch.object(auto_translate, "translate_batch", fake):
            auto_translate.translate(entries, cache, budget=10)
        self.assertLessEqual(len(sent), 10)


class OverlayMergeTests(unittest.TestCase):
    """mirror.load_translations puts the human file above the generated one."""

    def test_a_human_summary_is_never_replaced_by_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            human = os.path.join(directory, "translations.json")
            auto = os.path.join(directory, "autoTranslations.json")
            with open(human, "w", encoding="utf-8") as handle:
                json.dump({"translations": {"x": {"summary": "Human Name"}}}, handle)
            with open(auto, "w", encoding="utf-8") as handle:
                json.dump({"translations": {"x": {"summary": "Model Name"}}}, handle)
            merged = mirror.load_translations(human, auto)
        self.assertEqual("Human Name", merged["x"]["summary"])

    def test_the_merge_is_per_field_not_per_addon(self):
        # Correcting only a summary must not throw away a generated
        # description that was fine.
        with tempfile.TemporaryDirectory() as directory:
            human = os.path.join(directory, "translations.json")
            auto = os.path.join(directory, "autoTranslations.json")
            with open(human, "w", encoding="utf-8") as handle:
                json.dump({"translations": {"x": {"summary": "Human Name"}}}, handle)
            with open(auto, "w", encoding="utf-8") as handle:
                json.dump({"translations": {
                    "x": {"summary": "Model Name", "description": "Model text."}
                }}, handle)
            merged = mirror.load_translations(human, auto)
        self.assertEqual("Human Name", merged["x"]["summary"])
        self.assertEqual("Model text.", merged["x"]["description"])

    def test_a_missing_generated_overlay_is_harmless(self):
        with tempfile.TemporaryDirectory() as directory:
            human = os.path.join(directory, "translations.json")
            with open(human, "w", encoding="utf-8") as handle:
                json.dump({"translations": {"x": {"summary": "Human Name"}}}, handle)
            merged = mirror.load_translations(
                human, os.path.join(directory, "nope.json"))
        self.assertEqual("Human Name", merged["x"]["summary"])

    def test_both_missing_yields_an_empty_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual({}, mirror.load_translations(
                os.path.join(directory, "a.json"),
                os.path.join(directory, "b.json"),
            ))


if __name__ == "__main__":
    unittest.main()
