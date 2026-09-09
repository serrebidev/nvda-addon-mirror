"""Non-English locales must carry translations, not copies of English."""

import json
import unittest
from unittest import mock
from urllib.error import HTTPError

import mirror


def httpError(url, code, message):
    """Build an HTTPError without leaking its response body."""
    error = HTTPError(url, code, message, None, None)
    error.close()
    return error


def addon(addon_id, channel="stable", name="Clock", desc="A clock."):
    return {
        "addonId": addon_id,
        "channel": channel,
        "displayName": name,
        "description": desc,
    }


class LocalizeCatalogTests(unittest.TestCase):
    def test_english_is_returned_untouched(self):
        output = [addon("clock")]
        self.assertIs(output, mirror.localize_catalog(output, "en", {}, {}, {}))

    def test_official_translation_is_preferred_over_everything(self):
        output = [addon("clock")]
        row = mirror._translation_row(output[0])
        localized = mirror.localize_catalog(
            output,
            "fr",
            {"fr": {row: {"description": "Une horloge."}}},
            {row: {"fr": {"description": "Depuis le paquet."}}},
            {mirror.translation_key("A clock.", "fr"): "Machine."},
        )
        self.assertEqual("Une horloge.", localized[0]["description"])

    def test_bundle_translation_is_used_when_the_store_has_none(self):
        output = [addon("clock")]
        row = mirror._translation_row(output[0])
        localized = mirror.localize_catalog(
            output,
            "fr",
            {},
            {row: {"fr": {"description": "Depuis le paquet."}}},
            {mirror.translation_key("A clock.", "fr"): "Machine."},
        )
        self.assertEqual("Depuis le paquet.", localized[0]["description"])

    def test_machine_translation_is_the_last_resort(self):
        output = [addon("clock")]
        localized = mirror.localize_catalog(
            output, "fr", {}, {},
            {mirror.translation_key("A clock.", "fr"): "Machine."},
        )
        self.assertEqual("Machine.", localized[0]["description"])

    def test_an_untranslated_addon_keeps_its_english(self):
        # Never worse than today: a gap shows the English string, not a blank.
        localized = mirror.localize_catalog([addon("clock")], "fr", {}, {}, {})
        self.assertEqual("A clock.", localized[0]["description"])
        self.assertEqual("Clock", localized[0]["displayName"])

    def test_channels_do_not_borrow_each_others_wording(self):
        # The official store lists one row per channel. Keying translations on
        # addonId alone let a dev row publish the stable row's text.
        stable = addon("robE", "stable", desc="Stable notes.")
        dev = addon("robE", "dev", desc="Dev notes.")
        official = {
            "fr": {mirror._translation_row(stable): {"description": "Notes stables."}}
        }
        localized = mirror.localize_catalog([stable, dev], "fr", official, {}, {})
        self.assertEqual("Notes stables.", localized[0]["description"])
        self.assertEqual("Dev notes.", localized[1]["description"])

    def test_regional_locales_fall_back_the_way_nvda_does(self):
        # Mirrors addonHandler._translatedManifestPaths: pt_BR, then pt.
        output = [addon("clock")]
        row = mirror._translation_row(output[0])
        localized = mirror.localize_catalog(
            output, "pt_BR", {"pt": {row: {"description": "Um relogio."}}}, {}, {},
        )
        self.assertEqual("Um relogio.", localized[0]["description"])

    def test_a_regional_translation_beats_the_base_language(self):
        output = [addon("clock")]
        row = mirror._translation_row(output[0])
        localized = mirror.localize_catalog(
            output,
            "pt_BR",
            {
                "pt_BR": {row: {"description": "Brasil."}},
                "pt": {row: {"description": "Portugal."}},
            },
            {},
            {},
        )
        self.assertEqual("Brasil.", localized[0]["description"])


class OfficialLocaleFetchTests(unittest.TestCase):
    def test_only_genuinely_translated_strings_are_recorded(self):
        # A language whose view repeats the English must not be stored as a
        # translation, or the mirror publishes English under that language's
        # URL and reports coverage it does not have.
        baseline = mirror._english_baseline([addon("clock"), addon("other")])
        payload = json.dumps(
            [addon("clock"), addon("other", desc="Une autre.")]
        ).encode()
        with mock.patch.object(
            mirror, "http_get_conditional", return_value=(payload, '"e"', None)
        ):
            translations, record = mirror.fetch_official_locale_translations(
                "fr", baseline
            )
        self.assertEqual(
            {mirror._translation_row(addon("other")): {"description": "Une autre."}},
            translations,
        )
        self.assertEqual('"e"', record["etag"])

    def test_recorded_keys_are_json_serialisable(self):
        # The cache is written to localeCache.json; tuple keys raise there.
        baseline = mirror._english_baseline([addon("clock")])
        payload = json.dumps([addon("clock", desc="Une horloge.")]).encode()
        with mock.patch.object(
            mirror, "http_get_conditional", return_value=(payload, None, None)
        ):
            _translations, record = mirror.fetch_official_locale_translations(
                "fr", baseline
            )
        json.dumps(record)

    def test_an_unchanged_language_reuses_its_cached_translations(self):
        cached = {
            "etag": '"e"',
            "translations": {"clock\tstable": {"description": "x"}},
        }
        with mock.patch.object(
            mirror,
            "http_get_conditional",
            side_effect=httpError("https://example.invalid", 304, "Not Modified"),
        ):
            translations, record = mirror.fetch_official_locale_translations(
                "fr", {}, cached
            )
        self.assertEqual(cached["translations"], translations)
        self.assertEqual('"e"', record["etag"])

    def test_a_failed_language_keeps_its_last_good_translations(self):
        cached = {
            "etag": '"e"',
            "translations": {"clock\tstable": {"description": "x"}},
        }
        with mock.patch.object(
            mirror, "http_get_conditional", side_effect=OSError("offline")
        ):
            translations, _record = mirror.fetch_official_locale_translations(
                "fr", {}, cached
            )
        self.assertEqual(cached["translations"], translations)

    def test_the_baseline_comes_from_the_stores_own_english(self):
        payload = json.dumps([addon("clock")]).encode()
        with mock.patch.object(
            mirror, "http_get_conditional", return_value=(payload, '"e"', None)
        ):
            baseline, record = mirror.fetch_official_english_baseline()
        self.assertEqual(
            {"clock\tstable": {"displayName": "Clock", "description": "A clock."}},
            baseline,
        )
        json.dumps(record)


class BundleTranslationTests(unittest.TestCase):
    def test_english_locale_directories_are_not_treated_as_translations(self):
        import io
        import zipfile

        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("manifest.ini", "name = clock\nversion = 1.0\n")
            archive.writestr("locale/en/manifest.ini", 'summary = "Clock"\n')
            archive.writestr("locale/fr/manifest.ini", 'summary = "Horloge"\n')
        self.assertEqual(
            {"fr": {"displayName": "Horloge"}},
            mirror.bundle_locale_translations(raw.getvalue()),
        )

    def test_unreadable_bundles_yield_nothing_instead_of_raising(self):
        for raw in (b"", None, b"not a zip"):
            self.assertEqual({}, mirror.bundle_locale_translations(raw))


class MachineTranslationTests(unittest.TestCase):
    def test_inert_without_credentials(self):
        cache = {}
        with mock.patch.object(mirror, "TRANSLATE_API_KEY", ""):
            spent = mirror.machine_translate_missing([("fr", "A clock.")], cache)
        self.assertEqual(0, spent)
        self.assertEqual({}, cache)

    def test_the_character_budget_is_respected(self):
        cache = {}

        def fake(texts, target, timeout=60):
            return [f"[{target}] {t}" for t in texts]

        needed = [("fr", "x" * 400), ("fr", "y" * 400), ("fr", "z" * 400)]
        with mock.patch.object(mirror, "TRANSLATE_API_KEY", "key"), mock.patch.object(
            mirror, "machine_translate_batch", fake
        ):
            spent = mirror.machine_translate_missing(needed, cache, budget=900)
        self.assertLessEqual(spent, 900)
        self.assertTrue(cache)
        self.assertLess(len(cache), len(needed))

    def test_a_failed_batch_is_discarded_rather_than_misaligned(self):
        # Pairing a short response positionally would publish one add-on's
        # description under another add-on's name.
        cache = {}
        with mock.patch.object(mirror, "TRANSLATE_API_KEY", "key"), mock.patch.object(
            mirror, "machine_translate_batch", lambda *a, **k: None
        ):
            mirror.machine_translate_missing([("fr", "A clock.")], cache)
        self.assertEqual("", cache.get(mirror.translation_key("A clock.", "fr"), ""))

    def test_translated_text_is_cached_under_its_source_and_language(self):
        cache = {}
        with mock.patch.object(mirror, "TRANSLATE_API_KEY", "key"), mock.patch.object(
            mirror,
            "machine_translate_batch",
            lambda texts, target, timeout=60: ["Une horloge."],
        ):
            mirror.machine_translate_missing([("fr", "A clock.")], cache)
        self.assertEqual(
            "Une horloge.", cache[mirror.translation_key("A clock.", "fr")]
        )

    def test_already_cached_text_is_never_sent_again(self):
        cache = {mirror.translation_key("A clock.", "fr"): "Une horloge."}
        with mock.patch.object(mirror, "TRANSLATE_API_KEY", "key"), mock.patch.object(
            mirror, "machine_translate_batch"
        ) as provider:
            spent = mirror.machine_translate_missing([("fr", "A clock.")], cache)
        provider.assert_not_called()
        self.assertEqual(0, spent)

    def test_every_nvda_locale_can_be_targeted(self):
        # A general model needs a language, not a code from a provider's
        # supported list, so no locale falls outside it. "kmr" used to.
        for lang in ("kmr", "pt_BR", "fr", "my", "ckb", "kok"):
            with self.subTest(lang=lang):
                self.assertEqual(lang, mirror.machine_translation_target(lang))

    def test_english_is_never_a_translation_target(self):
        for lang in ("en", "en_GB", "", None):
            with self.subTest(lang=lang):
                self.assertIsNone(mirror.machine_translation_target(lang))

    def test_a_short_or_reordered_reply_is_discarded_whole(self):
        # One answer per input, or nothing: a partial reply would attach one
        # add-on's text to another add-on's name.
        body = {"choices": [{"message": {"content": json.dumps({"0": "un"})}}]}

        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return json.dumps(body).encode()

        with mock.patch.object(mirror, "TRANSLATE_API_KEY", "key"),                 mock.patch.object(mirror, "urlopen", return_value=FakeResponse()):
            self.assertIsNone(
                mirror.machine_translate_batch(["one", "two"], "fr")
            )

    def test_an_aligned_reply_is_returned_positionally(self):
        body = {"choices": [{"message": {"content":
                json.dumps({"0": "un", "1": "deux"})}}]}

        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return json.dumps(body).encode()

        with mock.patch.object(mirror, "TRANSLATE_API_KEY", "key"),                 mock.patch.object(mirror, "urlopen", return_value=FakeResponse()):
            self.assertEqual(
                ["un", "deux"],
                mirror.machine_translate_batch(["one", "two"], "fr"),
            )


class TranslationGapTests(unittest.TestCase):
    def test_gaps_skip_anything_a_human_already_translated(self):
        stable = addon("clock")
        row = mirror._translation_row(stable)
        gaps = list(
            mirror.translation_gaps(
                [stable], "fr", {"fr": {row: {"description": "Une horloge."}}}, {}
            )
        )
        # The description is covered by the store; the displayName is not.
        self.assertEqual([("fr", "Clock")], gaps)

    def test_a_bundle_translation_also_closes_the_gap(self):
        stable = addon("clock")
        row = mirror._translation_row(stable)
        gaps = list(
            mirror.translation_gaps(
                [stable], "fr", {}, {row: {"fr": {"description": "Une horloge."}}}
            )
        )
        self.assertEqual([("fr", "Clock")], gaps)


if __name__ == "__main__":
    unittest.main()
