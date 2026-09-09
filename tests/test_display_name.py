"""An add-on's store entry must be titled with its name, not its description.

manifest.ini's ``summary`` is the field NVDA shows as the add-on's name, and
authors sometimes write a description there instead. Published verbatim that
leaves a paragraph where a title belongs and the real name nowhere at all,
which is unusable when arrowing a list of names.
"""

import unittest

import mirror


class ProseDetectionTests(unittest.TestCase):
    # Real names taken from the live catalog. None of these may be flagged:
    # a false positive replaces a correct title with a reconstructed one.
    REAL_NAMES = [
        "Terminal Access",
        "Acapela TTS Voices for NVDA Engines",
        "BOA: Better Office Accessibility",
        "Cursor Locator",
        "PowerBox: Essential Windows Productivity Tools",
        "BraiLab PC Speech Synthesizer",
        "Liste Icones Zone Notification (Notification Area Icons List)",
        "Emoticons",
        "SAPI5 Organizer",
        "Text Marks the Spot",
        "Gestor de BIOS y UEFI Accesible",
        "Monitor del Sistema",
        "The Clock",
        "AI Email Composer Pro",
    ]

    # Real published values that are descriptions, not names.
    PROSE = [
        "Accessible add-on for NVDA that downloads MP3 audio, MP4 video, and playlists.",
        "A powerful multi-tool add-on for NVDA featuring gesture layers, smart terminal launches, and media controls.",
        "Add-on for NVDA that lets you view, search, and modify BIOS and UEFI parameters directly from Windows.",
        "Check the current weather and your city's forecast with accessible NVDA keyboard shortcuts.",
        "NVDA add-on for advanced text search in files, documents, and images.",
        "An NVDA add-on for announcing Lenovo Fn shortcut states.",
        "This add-on allows to listen the typed symbols.",
        "Allows braille to be entered via the PC keyboard.",
        "Use a quick nav-like functionality to manage objects, such as navigating.",
        "lets you jump to any line on a text",
    ]

    def test_real_names_are_not_flagged(self):
        for name in self.REAL_NAMES:
            with self.subTest(name=name):
                self.assertFalse(mirror.looks_like_prose_name(name))

    def test_descriptions_are_flagged(self):
        for text in self.PROSE:
            with self.subTest(text=text):
                self.assertTrue(mirror.looks_like_prose_name(text))

    def test_blank_input_is_not_prose(self):
        for value in ("", None, "   "):
            self.assertFalse(mirror.looks_like_prose_name(value))

    def test_length_alone_never_decides(self):
        # Long noun phrases are ordinary add-on names; only a grammatical
        # signal may flag one.
        self.assertFalse(
            mirror.looks_like_prose_name("Accessible Media Converter for NVDA")
        )


class HumanizedIdTests(unittest.TestCase):
    def test_camel_case_and_separators_become_words(self):
        cases = {
            "biosManager": "Bios Manager",
            "ricerca_testuale_accesso_digitale": "Ricerca Testuale Accesso Digitale",
            "contrast-checker-nvda": "Contrast Checker NVDA",
            "copyURL": "Copy URL",
            # camelCase splits the same way whatever the first letter's case.
            "PowerBox": "Power Box",
            "internet_speed_checker": "Internet Speed Checker",
        }
        for addon_id, expected in cases.items():
            with self.subTest(addon_id=addon_id):
                self.assertEqual(expected, mirror.humanized_addon_id(addon_id))

    def test_empty_id_yields_empty(self):
        self.assertEqual("", mirror.humanized_addon_id(""))


class LeadingNameSegmentTests(unittest.TestCase):
    def test_a_name_before_its_tagline_is_recovered(self):
        cases = {
            "AILiveTranslate (real-time speech translation powered by Gemini)":
                "AILiveTranslate",
            "OmniTranslate - High-speed accessible translation add-on":
                "OmniTranslate",
            "TextInfo: counting standard pages, characters, words":
                "TextInfo",
            "SoundTub - download acessivel de audio e video": "SoundTub",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, mirror.leading_name_segment(value))

    def test_a_sentence_containing_a_dash_is_not_chopped_up(self):
        self.assertEqual(
            "",
            mirror.leading_name_segment(
                "An NVDA add-on that lets you do things - and more things"
            ),
        )

    def test_plain_names_have_no_segment_to_take(self):
        self.assertEqual("", mirror.leading_name_segment("Cursor Locator"))


class BestDisplayNameTests(unittest.TestCase):
    def test_a_usable_catalog_name_is_kept(self):
        # A curated or translated name must win over everything else.
        self.assertEqual(
            "Copy URL",
            mirror.best_display_name("Copy URL", "Something Else", "copyURL"),
        )

    def test_the_manifest_name_replaces_a_description(self):
        self.assertEqual(
            "ObjPad",
            mirror.best_display_name(
                "Use a quick nav-like functionality to manage objects, such as this.",
                "ObjPad",
                "objPad",
            ),
        )

    def test_the_id_is_used_when_every_source_is_a_description(self):
        self.assertEqual(
            "Bios Manager",
            mirror.best_display_name(
                "Add-on for NVDA that lets you view and modify BIOS parameters.",
                "An NVDA add-on that lets you edit firmware settings.",
                "biosManager",
            ),
        )

    def test_a_name_with_a_tagline_beats_the_reconstructed_id(self):
        self.assertEqual(
            "AILiveTranslate",
            mirror.best_display_name(
                "AILiveTranslate (real-time speech translation that you can use)",
                "",
                "ailivetranslate",
            ),
        )

    def test_a_description_is_never_published_as_a_name(self):
        prose = "An NVDA add-on that announces things you care about."
        self.assertNotEqual(prose, mirror.best_display_name(prose, prose, "thing"))


class TransformDisplayNameTests(unittest.TestCase):
    @staticmethod
    def _entry(**kwargs):
        base = {
            "name": "objPad",
            "version": "1.0",
            "summary": "Use a quick nav-like functionality to manage objects, such as this.",
            "description": "Perform various NVDA object commands.",
            "channel": "stable",
            "source": "github_owner",
        }
        base.update(kwargs)
        return base

    def test_a_prose_summary_does_not_reach_displayname(self):
        obj = mirror.transform(self._entry(manifest_summary="ObjPad"), "0" * 64)
        self.assertEqual("ObjPad", obj["displayName"])

    def test_the_description_is_left_alone(self):
        obj = mirror.transform(self._entry(manifest_summary="ObjPad"), "0" * 64)
        self.assertEqual("Perform various NVDA object commands.", obj["description"])

    def test_a_good_summary_still_wins(self):
        obj = mirror.transform(
            self._entry(summary="ObjPad", manifest_summary="Something Else"),
            "0" * 64,
        )
        self.assertEqual("ObjPad", obj["displayName"])

    def test_no_name_anywhere_still_produces_a_title(self):
        obj = mirror.transform(
            self._entry(summary="", description="", manifest_summary=""), "0" * 64
        )
        self.assertEqual("Obj Pad", obj["displayName"])


class ManifestSummaryCaptureTests(unittest.TestCase):
    @staticmethod
    def _bundle(manifest):
        import io
        import zipfile

        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("manifest.ini", manifest)
        return raw.getvalue()

    def test_the_name_is_read_from_the_bundle(self):
        bundle = self._bundle('name = "objPad"\nsummary = "ObjPad"\n')
        self.assertEqual("ObjPad", mirror.bundle_manifest_summary(bundle))

    def test_unreadable_bundles_yield_nothing_instead_of_raising(self):
        for raw in (b"", None, b"not a zip", self._bundle("name = x\n")):
            self.assertEqual("", mirror.bundle_manifest_summary(raw))


if __name__ == "__main__":
    unittest.main()


class NonEnglishNameTests(unittest.TestCase):
    NON_ENGLISH = [
        "Gestor de BIOS y UEFI Accesible",
        "Monitor del Sistema",
        "RadioTV - Đài phát thanh và truyền hình Việt Nam",
        "SoundTub - download acessível de áudio e vídeo",
        "Часы",
    ]
    ENGLISH = [
        "Accessible BIOS and UEFI Manager",
        "System Monitor",
        "Cursor Locator",
        "Sound Dictionaries",
        "Audio Media Converter",
        "Radio and TV Player",
        "PowerBox: Essential Windows Productivity Tools",
    ]

    def test_non_english_names_are_detected(self):
        for name in self.NON_ENGLISH:
            with self.subTest(name=name):
                self.assertTrue(mirror.looks_non_english_name(name))

    def test_english_names_are_not(self):
        for name in self.ENGLISH:
            with self.subTest(name=name):
                self.assertFalse(mirror.looks_non_english_name(name))


class ComposeDisplayNameTests(unittest.TestCase):
    def test_english_comes_first_with_the_original_after_aka(self):
        self.assertEqual(
            "Accessible BIOS and UEFI Manager, AKA Gestor de BIOS y UEFI Accesible",
            mirror.compose_display_name(
                "Accessible BIOS and UEFI Manager", "Gestor de BIOS y UEFI Accesible"
            ),
        )

    def test_an_english_original_gets_no_aka(self):
        self.assertEqual(
            "Cursor Locator",
            mirror.compose_display_name("Cursor Locator", "Cursor Locator Pro"),
        )

    def test_a_foreign_tagline_is_not_a_second_name(self):
        # "SoundTub" and "SoundTub - download acessivel..." are one name plus a
        # subtitle; repeating it after AKA would be noise.
        self.assertEqual(
            "SoundTub",
            mirror.compose_display_name(
                "SoundTub", "SoundTub - download acessível de áudio e vídeo"
            ),
        )

    def test_identical_names_are_not_doubled(self):
        self.assertEqual(
            "Monitor del Sistema",
            mirror.compose_display_name("Monitor del Sistema", "Monitor del Sistema"),
        )

    def test_no_english_name_leaves_the_original_alone(self):
        self.assertEqual(
            "Monitor del Sistema",
            mirror.compose_display_name("", "Monitor del Sistema"),
        )

    def test_a_non_english_english_side_is_left_alone(self):
        # Nothing has been translated yet, so there is no pair to show.
        self.assertEqual(
            "Gestor de BIOS",
            mirror.compose_display_name("Gestor de BIOS", "Monitor del Sistema"),
        )

    def test_a_missing_original_is_harmless(self):
        self.assertEqual("System Monitor", mirror.compose_display_name("System Monitor", ""))


class TransformAkaTests(unittest.TestCase):
    def test_the_published_name_carries_both(self):
        entry = {
            "name": "monitorSistema",
            "version": "1.0",
            # What the English overlay supplies.
            "summary": "System Monitor",
            "description": "Accessible resource monitor for NVDA.",
            "manifest_summary": "Monitor del Sistema",
            "channel": "stable",
            "source": "bestmidi",
        }
        obj = mirror.transform(entry, "0" * 64)
        self.assertEqual(
            "System Monitor, AKA Monitor del Sistema", obj["displayName"]
        )

    def test_an_ordinary_english_addon_is_untouched(self):
        entry = {
            "name": "objPad",
            "version": "1.0",
            "summary": "ObjPad",
            "description": "Perform various NVDA object commands.",
            "manifest_summary": "ObjPad",
            "channel": "stable",
            "source": "github_owner",
        }
        self.assertEqual("ObjPad", mirror.transform(entry, "0" * 64)["displayName"])

    def test_a_composed_name_is_not_mistaken_for_prose(self):
        # Two titles joined are longer than either, but still a pair of names.
        composed = mirror.compose_display_name(
            "Accessible BIOS and UEFI Manager", "Gestor de BIOS y UEFI Accesible"
        )
        self.assertIn(mirror.AKA_SEPARATOR, composed)
        self.assertFalse(mirror.looks_like_prose_name(composed))

    def test_a_composed_name_with_a_prose_english_half_is_still_flagged(self):
        self.assertTrue(
            mirror.looks_like_prose_name(
                "An NVDA add-on that lets you do things"
                + mirror.AKA_SEPARATOR
                + "Monitor del Sistema"
            )
        )


class RuChannelLabelTests(unittest.TestCase):
    """nvda-addons.ru marks 89% of its catalog "Dev", which carries no signal.

    Taken at face value it hides several hundred ordinary add-ons from the
    Stable view NVDA's Add-on Store shows by default.
    """

    def test_an_uncorroborated_dev_label_is_treated_as_stable(self):
        for version in ("1.0.0", "2025.1", "1.9.5", "2026.8.5", "3.1.4",
                        "19.1.3-RS", "1.2.1009.12"):
            with self.subTest(version=version):
                self.assertEqual("stable", mirror._norm_channel_ru("Dev", version))

    def test_a_version_that_says_pre_release_keeps_the_label(self):
        for version, expected in (
            ("1.0.0-beta", "dev"), ("2.0-rc1", "dev"), ("0.5dev", "dev"),
            ("1.0.0alpha2", "dev"), ("1.0-preview", "dev"),
        ):
            with self.subTest(version=version):
                self.assertEqual(expected, mirror._norm_channel_ru("Dev", version))

    def test_a_beta_label_needs_the_same_corroboration(self):
        self.assertEqual("stable", mirror._norm_channel_ru("Beta", "1.0.0"))
        self.assertEqual("beta", mirror._norm_channel_ru("Beta", "1.0.0-beta"))

    def test_an_explicit_stable_label_is_always_honoured(self):
        self.assertEqual("stable", mirror._norm_channel_ru("Stable", "1.0.0-beta"))

    def test_without_a_version_the_label_is_taken_at_face_value(self):
        # Callers that have no version to check with must not be silently
        # promoted; this keeps the old behaviour for them.
        self.assertEqual("dev", mirror._norm_channel_ru("Dev", None))

    def test_a_marker_inside_a_word_is_not_a_pre_release(self):
        for version in ("Ardev 1.0", "1.0 development build", "1.0"):
            with self.subTest(version=version):
                self.assertFalse(mirror.version_marks_prerelease(version))
