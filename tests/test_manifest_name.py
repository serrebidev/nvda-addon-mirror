import unittest

import mirror


class ApplyManifestNamesTests(unittest.TestCase):
    """The published addonId must be the bundle's manifest ``name``.

    NVDA records a store compatibility override under the catalog's
    addonId but checks it against the downloaded bundle's manifest name,
    so a catalog that publishes a display name (or another add-on's name)
    makes the add-on uninstallable from the store while the same file
    installs fine by hand.
    """

    def _entry(self, name, source="bestmidi", url="https://example.invalid/a"):
        return {"name": name, "source": source, "download_url": url}

    def test_display_name_id_is_replaced_by_the_manifest_name(self):
        entries = [self._entry("Eloquence-C-Hörtest (x64-DLL)")]
        cache = {entries[0]["download_url"]: {"manifest_name": "reloqCExperimental"}}
        self.assertEqual(1, mirror.apply_manifest_names(entries, cache))
        self.assertEqual("reloqCExperimental", entries[0]["name"])

    def test_matching_name_is_left_alone(self):
        entries = [self._entry("audioEqualizer")]
        cache = {entries[0]["download_url"]: {"manifest_name": "audioEqualizer"}}
        self.assertEqual(0, mirror.apply_manifest_names(entries, cache))
        self.assertEqual("audioEqualizer", entries[0]["name"])

    def test_official_and_pinned_entries_are_not_touched(self):
        for source in ("official", "pinned"):
            entries = [self._entry("Catalog Name", source=source)]
            cache = {entries[0]["download_url"]: {"manifest_name": "manifestName"}}
            self.assertEqual(0, mirror.apply_manifest_names(entries, cache))
            self.assertEqual("Catalog Name", entries[0]["name"])

    def test_no_cached_manifest_name_keeps_the_catalog_name(self):
        entries = [self._entry("Some Add-on")]
        for cache in ({}, {entries[0]["download_url"]: {}},
                      {entries[0]["download_url"]: {"manifest_name": ""}}):
            self.assertEqual(0, mirror.apply_manifest_names(entries, cache))
            self.assertEqual("Some Add-on", entries[0]["name"])

    def test_template_names_are_not_published_as_ids(self):
        entries = [self._entry("Some Add-on")]
        cache = {entries[0]["download_url"]: {"manifest_name": "addonTemplate"}}
        self.assertEqual(0, mirror.apply_manifest_names(entries, cache))
        self.assertEqual("Some Add-on", entries[0]["name"])

    def test_manifest_name_is_sanitized_like_any_other_id(self):
        entries = [self._entry("Some Add-on")]
        cache = {entries[0]["download_url"]: {"manifest_name": "weird:name"}}
        self.assertEqual(1, mirror.apply_manifest_names(entries, cache))
        self.assertEqual("weird-name", entries[0]["name"])


if __name__ == "__main__":
    unittest.main()
