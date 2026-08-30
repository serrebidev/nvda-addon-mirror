import json
import os
import tempfile
import unittest
from unittest import mock

import mirror


class PinnedVersionTests(unittest.TestCase):
    def test_newer_asset_version_wins_over_stale_manifest(self):
        self.assertEqual(
            "2.0.0",
            mirror._select_pinned_version(
                "1.5.0",
                "pantheraspeech-2.0.0.nvda-addon",
                "pantheraspeech/v2.0.0",
            ),
        )

    def test_manifest_spelling_is_kept_when_versions_match(self):
        self.assertEqual(
            "19.1.3-RS",
            mirror._select_pinned_version(
                "19.1.3-RS",
                "Eloquence-19.1.3-RS.nvda-addon",
                "v19.1.3-RS",
            ),
        )


class PinnedCompletenessTests(unittest.TestCase):
    def test_failed_pin_stops_incomplete_publication(self):
        config = {
            "pinned": [
                {"repo": "example/good", "addon_id": "good"},
                {"repo": "example/bad", "addon_id": "bad"},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pinned.json")
            with open(path, "w", encoding="utf-8") as config_file:
                json.dump(config, config_file)

            def fetch(spec, repo, addon_id):
                if repo == "example/bad":
                    raise RuntimeError("release unavailable")
                return [{"name": addon_id}]

            with mock.patch.object(mirror, "_fetch_one_pinned", side_effect=fetch):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "refusing to publish an incomplete pinned add-on set",
                ):
                    mirror.fetch_pinned(path)


class DedupeTests(unittest.TestCase):
    def test_pinned_release_wins_over_stale_bestmidi_metadata(self):
        shared = {
            "name": "pantheraspeech",
            "channel": "stable",
            "download_url": "https://example.invalid/panthera.nvda-addon",
            "summary": "Panthera Speech",
            "description": "",
        }
        bestmidi = dict(shared, source="bestmidi", version="1.5.0")
        pinned = dict(shared, source="pinned", version="2.0.1")

        self.assertEqual("2.0.1", mirror.dedupe([bestmidi, pinned])[0]["version"])

    def test_addon_ids_are_deduplicated_case_insensitively(self):
        shared = {
            "channel": "stable",
            "download_url": "https://example.invalid/addon.nvda-addon",
            "summary": "Example",
            "description": "",
        }
        official = dict(shared, name="ExampleAddon", source="official")
        community = dict(shared, name="exampleaddon", source="bestmidi")

        result = mirror.dedupe([community, official])
        self.assertEqual(1, len(result))
        self.assertEqual("ExampleAddon", result[0]["name"])


class SpanishCatalogTests(unittest.TestCase):
    def test_aliases_are_not_mistaken_for_original_addons(self):
        official = {"name": "ifInterpreters", "source": "official"}
        alias = {
            "name": "IF Interpreters",
            "catalog_name": "IF Interpreters",
            "catalog_file": "ifinterpreters",
            "source": "es",
        }
        original = {
            "name": "newSpanishAddon",
            "catalog_name": "newSpanishAddon",
            "catalog_file": "newspanishaddon",
            "source": "es",
        }

        result = mirror.keep_original_es_entries([official, alias, original])
        self.assertEqual([official, original], result)

    def test_known_product_name_maps_to_internal_id(self):
        community = {"name": "codefactory-py3", "source": "ru"}
        spanish = {
            "name": "codefactory",
            "catalog_name": "codefactory",
            "catalog_file": "codefactory",
            "source": "es",
        }

        self.assertEqual(
            [community],
            mirror.keep_original_es_entries([community, spanish]),
        )


class TranslationTests(unittest.TestCase):
    def test_edge_reader_description_is_english(self):
        translations = mirror.load_translations()
        description = translations["edgeReader"]["description"]

        self.assertIn("automatically saves any text", description)
        self.assertNotRegex(description, r"[\u0400-\u04ff]")


if __name__ == "__main__":
    unittest.main()
