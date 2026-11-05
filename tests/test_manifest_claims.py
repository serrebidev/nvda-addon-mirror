"""A catalog entry is a claim; the bundle's manifest.ini is what NVDA obeys."""
import io
import unittest
import zipfile
from unittest import mock

import mirror

URL = "https://example.invalid/releases/addon.nvda-addon"


def bundle(manifest):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("manifest.ini", manifest)
    return out.getvalue()


GOOD = bundle(
    "name = realName\nversion = 1.2.3\n"
    "minimumNVDAVersion = 2024.1\nlastTestedNVDAVersion = 2026.2.0\n"
)


def entry(**fields):
    base = {"name": "realName", "source": "bestmidi", "download_url": URL,
            "version": "1.0", "min_nvda": (2019, 1, 0), "last_tested": (2019, 1, 0)}
    return dict(base, **fields)


class ManifestCaptureTests(unittest.TestCase):
    def download(self, body, **kwargs):
        with mock.patch.object(
            mirror, "sha256_stream",
            return_value=("a" * 64, len(body), None, None, body),
        ):
            return mirror.cached_download(
                {"download_url": URL, "version": "1.0"}, now=100,
                capture_limit=mirror.MANIFEST_CAPTURE_LIMIT, **kwargs,
            )

    def test_api_versions_are_captured_with_the_download(self):
        record, error = self.download(GOOD)
        self.assertIsNone(error)
        self.assertEqual("2024.1", record["manifest_min_nvda"])
        self.assertEqual("2026.2.0", record["manifest_last_tested"])

    def test_a_body_that_is_not_a_bundle_is_rejected(self):
        # addons.nvda-project.org now redirects to an HTML page. Hashing that
        # publishes a row NVDA verifies, then cannot open.
        for body in (b"<!doctype html><html></html>", bundle("version = 1.0\n")):
            record, error = self.download(body)
            self.assertIn("not an NVDA add-on bundle", error)
            self.assertNotIn("sha256", record)
            self.assertEqual(100 + mirror.DOWNLOAD_RECHECK_SECONDS,
                             record["next_check"])

    def test_a_rejected_body_is_not_refetched_before_its_recheck(self):
        body = b"<!doctype html>"
        record, _ = self.download(body)
        with mock.patch.object(mirror, "sha256_stream") as stream:
            again, error = mirror.cached_download(
                {"download_url": URL, "version": "1.0"}, record, now=200,
                capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
        stream.assert_not_called()
        self.assertIn("not an NVDA add-on bundle", error)
        self.assertNotIn("sha256", again)


class ApplyManifestClaimsTests(unittest.TestCase):
    def cache(self, **fields):
        return {URL: fields}

    def test_the_manifest_version_replaces_the_catalog_claim(self):
        # BestMidi listed A11y Task Recorder as 0.5.11 for a v0.2.0 download.
        item = entry(version="0.5.11")
        mirror.apply_manifest_claims([item], self.cache(manifest_version="0.2.0"))
        self.assertEqual("0.2.0", item["version"])
        self.assertEqual("0.5.11", item["catalog_version"])

    def test_an_equal_version_keeps_the_catalog_string(self):
        item = entry(version="6.0")
        mirror.apply_manifest_claims([item], self.cache(manifest_version="6.0.0"))
        self.assertEqual("6.0", item["version"])
        self.assertNotIn("catalog_version", item)

    def test_a_version_nvda_cannot_parse_is_not_published(self):
        for stated in ("ef7b3f4c0c03", "x.y", "2.8-2026.2", "0.7.8-alpha.1", ""):
            item = entry(version="2026.2")
            mirror.apply_manifest_claims([item], self.cache(manifest_version=stated))
            self.assertEqual("2026.2", item["version"], stated)

    def test_api_versions_follow_the_manifest(self):
        item = entry()
        mirror.apply_manifest_claims([item], self.cache(
            manifest_min_nvda="2024.1", manifest_last_tested="2026.2.0",
        ))
        self.assertEqual((2024, 1, 0), item["min_nvda"])
        self.assertEqual((2026, 2, 0), item["last_tested"])

    def test_a_blank_api_version_is_what_nvda_reads_it_as(self):
        # Claiming compatible offers a plain Install, then "Add-on not supported".
        item = entry(last_tested=(2027, 1, 0))
        mirror.apply_manifest_claims([item], self.cache(
            manifest_min_nvda="", manifest_last_tested="",
        ))
        self.assertEqual((0, 0, 0), item["min_nvda"])
        self.assertEqual((0, 0, 0), item["last_tested"])

    def test_nothing_captured_leaves_the_catalog_claim(self):
        item = entry()
        for cache in ({}, {URL: {}}, {URL: {"manifest_last_tested": None}}):
            mirror.apply_manifest_claims([item], cache)
            self.assertEqual((2019, 1, 0), item["last_tested"])
            self.assertEqual("1.0", item["version"])

    def test_official_and_pinned_rows_are_left_alone(self):
        for source in ("official", "pinned"):
            item = entry(source=source)
            mirror.apply_manifest_claims([item], self.cache(
                manifest_version="9.9", manifest_last_tested="2026.2",
            ))
            self.assertEqual("1.0", item["version"])
            self.assertEqual((2019, 1, 0), item["last_tested"])

    def test_a_corrected_version_does_not_force_a_download(self):
        # cached_download keys its recheck on the version; comparing the
        # corrected one would re-download every bundle this ever touches.
        item = entry(version="0.2.0", catalog_version="0.5.11")
        cached = {"sha256": "a" * 64, "size": 1, "version": "0.5.11",
                  "next_check": 999, "manifest_name": "n", "manifest_last_tested": ""}
        with mock.patch.object(mirror, "sha256_stream") as stream:
            record, error = mirror.cached_download(
                item, cached, now=100, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
        stream.assert_not_called()
        self.assertIsNone(error)
        self.assertEqual("0.5.11", record["version"])


class PublishedRowTests(unittest.TestCase):
    def test_a_version_name_is_safe_in_a_windows_file_name(self):
        # NVDA saves "{addonId}-{addonVersionName}.nvda-addon". A colon there
        # makes the final os.replace fail outside NVDA's error handling.
        item = entry(version="20.05.04:341d96a", summary="s")
        with mock.patch.object(mirror, "TRANSLATIONS", {}):
            row = mirror.transform(item, "a" * 64)
        self.assertEqual("20.05.04-341d96a", row["addonVersionName"])
        self.assertEqual(20, row["addonVersionNumber"]["major"])


class BlocklistTests(unittest.TestCase):
    def test_a_renamed_add_on_stays_blocked_under_its_catalog_name(self):
        item = {"name": "manifestName", "catalog_name": "Hacking by addon"}
        kept, rejected = mirror.drop_blocked_entries(
            [item], [{"name": "Hacking by addon", "reason": "malware"}],
        )
        self.assertEqual([], kept)
        self.assertEqual(1, len(rejected))


class ManifestParsingTests(unittest.TestCase):
    def test_single_quotes_are_not_part_of_the_name(self):
        self.assertEqual("realName", mirror._manifest_name("name = 'realName'\n"))

    def test_an_inline_comment_is_not_part_of_the_name(self):
        self.assertEqual("realName", mirror._manifest_name("name = realName # id\n"))


if __name__ == "__main__":
    unittest.main()
