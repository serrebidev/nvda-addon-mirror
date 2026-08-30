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


if __name__ == "__main__":
    unittest.main()
