"""Add-ons an author distributes from their own website.

No catalog lists these and they are not on GitHub, so nothing else in the
build can reach them: a pinned entry naming a plain URL is the only way in.
"""

import hashlib
import io
import tempfile
import unittest
import zipfile
from unittest import mock
from urllib.error import HTTPError, URLError

import mirror


def bundle(manifest):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("manifest.ini", manifest)
    return raw.getvalue()


MANIFEST = (
    "name = agnow\n"
    "summary = AGNow stream speech\n"
    "description = Speaks the stream.\n"
    "author = Brynify\n"
    "version = 1.0.1\n"
    "url = https://agnow.brynify.me\n"
    "minimumNVDAVersion = 2024.1\n"
    "lastTestedNVDAVersion = 2026.1\n"
)
URL = "https://agnow.brynify.me/agnow-1.0.1.nvda-addon"
MODIFIED = "Wed, 21 Oct 2026 07:28:00 GMT"


class PinnedURLConfigTests(unittest.TestCase):
    def test_a_spec_may_name_a_url_instead_of_a_repo(self):
        spec = {"url": URL, "addon_id": "agnow"}
        with mock.patch.object(mirror, "_load_pinned_config", return_value=[spec]), \
                mock.patch.object(mirror, "_fetch_one_pinned_url",
                                  return_value=[{"name": "agnow"}]) as fetch:
            entries = mirror._fetch_pinned_impl("ignored.json")
        fetch.assert_called_once_with(spec, URL, "agnow")
        self.assertEqual([{"name": "agnow"}], entries)

    def test_a_spec_with_neither_repo_nor_url_fails_the_build(self):
        with mock.patch.object(mirror, "_load_pinned_config",
                               return_value=[{"addon_id": "agnow"}]):
            with self.assertRaises(RuntimeError) as raised:
                mirror._fetch_pinned_impl("ignored.json")
        self.assertIn("missing addon_id and repo/url", str(raised.exception))

    def test_a_failing_url_names_the_url_in_the_failure(self):
        with mock.patch.object(mirror, "_load_pinned_config",
                               return_value=[{"url": URL, "addon_id": "agnow"}]), \
                mock.patch.object(mirror, "_fetch_one_pinned_url",
                                  side_effect=RuntimeError("gone")):
            with self.assertRaises(RuntimeError) as raised:
                mirror._fetch_pinned_impl("ignored.json")
        self.assertIn(URL, str(raised.exception))


class PinnedURLAssetTests(unittest.TestCase):
    @staticmethod
    def _head(headers):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = headers
        return response

    def test_validators_become_the_bundle_identity(self):
        with mock.patch.object(mirror, "urlopen", return_value=self._head(
            {"ETag": '"abc"', "Last-Modified": MODIFIED, "Content-Length": "4096"},
        )) as opened:
            asset, modified = mirror._pinned_url_asset(URL)
        self.assertEqual("HEAD", opened.call_args.args[0].get_method())
        self.assertEqual('"abc"', asset["id"])
        self.assertEqual(MODIFIED, asset["updated_at"])
        self.assertEqual("4096", asset["size"])
        self.assertEqual(URL, asset["browser_download_url"])
        self.assertEqual("agnow-1.0.1.nvda-addon", asset["name"])
        self.assertEqual(MODIFIED, modified)

    def test_a_replaced_file_gets_a_different_identity(self):
        seen = []
        for etag in ('"first"', '"second"'):
            with mock.patch.object(mirror, "urlopen",
                                   return_value=self._head({"ETag": etag})):
                seen.append(mirror._pinned_url_asset(URL)[0]["id"])
        self.assertNotEqual(seen[0], seen[1])

    def test_a_host_refusing_HEAD_still_yields_a_usable_asset(self):
        for failure in (URLError("offline"),
                        HTTPError(URL, 405, "Method Not Allowed", {}, None)):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(mirror, "urlopen", side_effect=failure):
                    asset, modified = mirror._pinned_url_asset(URL)
                self.assertEqual(URL, asset["browser_download_url"])
                self.assertIsNone(asset["id"])
                self.assertIsNone(modified)

    def test_a_percent_encoded_path_yields_the_real_file_name(self):
        with mock.patch.object(mirror, "urlopen", return_value=self._head({})):
            asset, _ = mirror._pinned_url_asset(
                "https://example.invalid/files/My%20Addon-2.3.4.nvda-addon",
            )
        self.assertEqual("My Addon-2.3.4.nvda-addon", asset["name"])


class PinnedURLEntryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patch = mock.patch.object(
            mirror, "PINNED_BUNDLE_CACHE_PATH", directory.name,
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.raw = bundle(MANIFEST)

    def _fetch(self, spec):
        asset = {"id": '"abc"', "updated_at": MODIFIED,
                 "size": str(len(self.raw)), "browser_download_url": URL,
                 "name": "agnow-1.0.1.nvda-addon"}
        with mock.patch.object(mirror, "_pinned_url_asset",
                               return_value=(asset, MODIFIED)), \
                mock.patch.object(mirror, "http_get", return_value=self.raw):
            return mirror._fetch_one_pinned_url(spec, URL, spec["addon_id"])[0]

    def test_metadata_comes_from_the_bundle_the_author_published(self):
        entry = self._fetch({"url": URL, "addon_id": "agnow"})
        self.assertEqual("agnow", entry["name"])
        self.assertEqual("1.0.1", entry["version"])
        self.assertEqual("AGNow stream speech", entry["summary"])
        self.assertEqual("Brynify", entry["author"])
        self.assertEqual("https://agnow.brynify.me", entry["homepage"])
        self.assertEqual((2024, 1, 0), entry["min_nvda"])
        self.assertEqual((2026, 1, 0), entry["last_tested"])
        self.assertEqual("pinned", entry["source"])
        self.assertEqual(mirror.parse_http_date_to_ms(MODIFIED),
                         entry["submission_ms"])

    def test_an_unrenamed_add_on_keeps_the_authors_own_download_url(self):
        """Rehosting an untouched bundle would take the author's install count."""
        entry = self._fetch({"url": URL, "addon_id": "agnow"})
        self.assertEqual(URL, entry["download_url"])
        self.assertNotIn("_patched_bytes", entry)
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), entry["sha256"])

    def test_a_renamed_add_on_is_repackaged_and_hashed_as_what_we_serve(self):
        entry = self._fetch({"url": URL, "addon_id": "agnow-variant"})
        self.assertIn("_patched_bytes", entry)
        patched = entry["_patched_bytes"]
        self.assertEqual(hashlib.sha256(patched).hexdigest(), entry["sha256"])
        with zipfile.ZipFile(io.BytesIO(patched)) as archive:
            manifest = archive.read("manifest.ini").decode("utf-8")
        self.assertEqual("agnow-variant", mirror._manifest_name(manifest))

    def test_the_spec_overrides_metadata_the_bundle_states_badly(self):
        entry = self._fetch({
            "url": URL, "addon_id": "agnow", "summary": "AGNow",
            "publisher": "Brynify", "channel": "beta",
            "homepage": "https://example.invalid/agnow",
            "license": "GPL v2", "changelog": "First public build.",
        })
        self.assertEqual("AGNow", entry["summary"])
        self.assertEqual("Brynify", entry["author"])
        self.assertEqual("beta", entry["channel"])
        self.assertEqual("https://example.invalid/agnow", entry["homepage"])
        self.assertEqual("GPL v2", entry["license"])
        self.assertEqual("First public build.", entry["changelog"])

    def test_a_template_named_bundle_is_refused(self):
        self.raw = bundle("name = addonTemplate\nversion = 1.0\n")
        with self.assertRaises(RuntimeError):
            self._fetch({"url": URL, "addon_id": "agnow"})

    def test_a_response_that_is_not_an_add_on_is_refused(self):
        self.raw = b"<html>404</html>"
        with self.assertRaises(zipfile.BadZipFile):
            self._fetch({"url": URL, "addon_id": "agnow"})


class HTTPDateTests(unittest.TestCase):
    def test_last_modified_becomes_epoch_milliseconds(self):
        self.assertEqual(1792567680000, mirror.parse_http_date_to_ms(MODIFIED))

    def test_a_missing_or_broken_header_is_not_an_error(self):
        for value in (None, "", "not a date", "Wed, 99 Xxx 2026 07:28:00 GMT"):
            with self.subTest(value=value):
                self.assertIsNone(mirror.parse_http_date_to_ms(value))


class PinnedURLProvenanceTests(unittest.TestCase):
    def test_a_website_pin_is_not_described_as_a_github_release(self):
        entry = {
            "name": "agnow", "version": "1.0.1", "summary": "AGNow",
            "channel": "stable", "source": "pinned",
            "store_source_label": mirror.PINNED_URL_SOURCE_LABEL,
            "download_url": URL,
        }
        self.assertEqual(
            "Author's website",
            mirror.transform(entry, "0" * 64)["storeSource"],
        )

    def test_a_github_pin_keeps_its_own_label(self):
        entry = {"name": "x", "version": "1.0", "channel": "stable",
                 "source": "pinned", "download_url": URL}
        self.assertEqual(
            "Pinned GitHub release",
            mirror.transform(entry, "0" * 64)["storeSource"],
        )
