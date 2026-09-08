import io
import tempfile
import unittest
import zipfile
from unittest import mock
from urllib.error import HTTPError, URLError

import mirror


class DownloadCacheTests(unittest.TestCase):
    def setUp(self):
        self.entry = {"download_url": "https://example.invalid/addon", "version": "1.0"}
        self.cached = {"sha256": "a" * 64, "size": 123, "version": "1.0",
                       "etag": '"old"', "next_check": 200}

    def test_warm_cache_makes_no_request(self):
        with mock.patch.object(mirror, "sha256_stream") as download:
            record, error = mirror.cached_download(self.entry, self.cached, now=100)
        download.assert_not_called()
        self.assertEqual(self.cached, record)
        self.assertIsNone(error)

    def test_legacy_cache_migrates_without_bulk_downloads(self):
        for legacy in ({"sha256": "a" * 64, "size": 123},
                       dict(self.cached)):
            legacy.pop("next_check", None)
            with mock.patch.object(mirror, "sha256_stream") as download:
                record, error = mirror.cached_download(self.entry, legacy, now=100)
            download.assert_not_called()
            self.assertEqual(100 + mirror.DOWNLOAD_RECHECK_SECONDS, record["next_check"])
            self.assertEqual("1.0", record["version"])
            self.assertIsNone(error)

    def test_new_version_bypasses_ttl_and_old_validators(self):
        entry = dict(self.entry, version="2.0")
        with mock.patch.object(mirror, "sha256_stream", return_value=("b" * 64, 123, '"new"', None, None)) as download:
            record, error = mirror.cached_download(entry, self.cached, now=100)
        download.assert_called_once_with(entry["download_url"], validators=None, capture_limit=0)
        self.assertEqual("b" * 64, record["sha256"])
        self.assertIsNone(error)

    def test_304_reuses_hash_and_renews_ttl(self):
        error = HTTPError(self.entry["download_url"], 304, "Not modified", {}, None)
        with mock.patch.object(mirror, "sha256_stream", side_effect=error) as download:
            record, failure = mirror.cached_download(self.entry, self.cached, now=201)
        download.assert_called_once_with(self.entry["download_url"], validators=self.cached, capture_limit=0)
        self.assertEqual(self.cached["sha256"], record["sha256"])
        self.assertEqual(201 + mirror.DOWNLOAD_RECHECK_SECONDS, record["next_check"])
        self.assertIsNone(failure)

    def test_host_without_validators_downloads_at_most_once_per_day(self):
        with mock.patch.object(mirror, "sha256_stream", return_value=("a" * 64, 123, None, None, None)) as download:
            record, _ = mirror.cached_download(self.entry, now=100)
            for now in range(200, 86000, 600):
                record, failure = mirror.cached_download(self.entry, record, now=now)
                self.assertIsNone(failure)
            download.assert_called_once()
            mirror.cached_download(self.entry, record, now=86500)
            self.assertEqual(2, download.call_count)

    def test_failure_is_backed_off_even_without_a_previous_hash(self):
        for previous in (None, self.cached):
            with mock.patch.object(mirror, "sha256_stream", side_effect=URLError("offline")) as download:
                record, failure = mirror.cached_download(self.entry, previous, now=201)
                self.assertIn("offline", failure)
                again, repeated = mirror.cached_download(self.entry, record, now=801)
                self.assertEqual(failure, repeated)
                self.assertEqual(record, again)
                download.assert_called_once()
                mirror.cached_download(self.entry, record, now=201 + mirror.DOWNLOAD_RETRY_SECONDS)
                self.assertEqual(2, download.call_count)

    def test_changed_version_failure_does_not_reuse_old_hash(self):
        with mock.patch.object(mirror, "sha256_stream", side_effect=URLError("offline")):
            record, error = mirror.cached_download(dict(self.entry, version="2"), self.cached, now=100)
        self.assertNotIn("sha256", record)
        self.assertIsNotNone(error)

    def test_success_after_failure_clears_error(self):
        cached = dict(self.cached, error="offline")
        with mock.patch.object(mirror, "sha256_stream", return_value=("b" * 64, 123, None, None, None)):
            record, error = mirror.cached_download(self.entry, cached, now=201)
        self.assertNotIn("error", record)
        self.assertIsNone(error)

    def test_force_bypasses_ttl_and_conditional_headers(self):
        with mock.patch.object(mirror, "sha256_stream", return_value=("b" * 64, 123, None, None, None)) as download:
            mirror.cached_download(self.entry, self.cached, force=True, now=100)
        download.assert_called_once_with(self.entry["download_url"], validators=None, capture_limit=0)

    def test_stream_uses_conditional_get_without_range(self):
        for validators, expected in (({"etag": '"old"'}, "If-none-match"),
                                     ({"last_modified": "yesterday"}, "If-modified-since")):
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {}
            response.read.side_effect = [b"test", b""]
            with mock.patch.object(mirror, "urlopen", return_value=response) as opened:
                _, size, _, _, _ = mirror.sha256_stream(self.entry["download_url"], validators=validators)
            request = opened.call_args.args[0]
            self.assertEqual("GET", request.get_method())
            self.assertIsNone(request.get_header("Range"))
            self.assertIsNotNone(request.get_header(expected))
            self.assertEqual(4, size)

    def test_pinned_bundle_reuses_bytes_until_asset_changes(self):
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.ini", "name = example\n")
        asset = {"id": 123, "updated_at": "first", "size": 100,
                 "browser_download_url": self.entry["download_url"]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(mirror, "PINNED_BUNDLE_CACHE_PATH", directory), \
                mock.patch.object(mirror, "http_get", return_value=bundle.getvalue()) as download:
            self.assertEqual(bundle.getvalue(), mirror.cached_pinned_bundle(asset))
            self.assertEqual(bundle.getvalue(), mirror.cached_pinned_bundle(asset))
            download.assert_called_once()
            mirror.cached_pinned_bundle(dict(asset, updated_at="replacement"))
            self.assertEqual(2, download.call_count)

    def test_invalid_pinned_bundle_is_not_persisted(self):
        asset = {"browser_download_url": self.entry["download_url"]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(mirror, "PINNED_BUNDLE_CACHE_PATH", directory), \
                mock.patch.object(mirror, "http_get", return_value=b"broken") as download:
            for _ in range(2):
                with self.assertRaises(zipfile.BadZipFile):
                    mirror.cached_pinned_bundle(asset)
            self.assertEqual(2, download.call_count)


class ManifestVersionRecoveryTests(unittest.TestCase):
    """A catalog that states no usable version still ships one in the bundle."""

    URL = "https://example.invalid/addon.nvda-addon"

    @staticmethod
    def _bundle(manifest):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("manifest.ini", manifest)
        return raw.getvalue()

    def test_manifest_version_is_read_from_the_download_being_hashed(self):
        bundle = self._bundle('name = example\nversion = 3.2.1\n')
        self.assertEqual("3.2.1", mirror.bundle_manifest_version(bundle))

    def test_unreadable_bundles_yield_no_version_instead_of_raising(self):
        for raw in (b"", b"not a zip", self._bundle("name = example\n")):
            self.assertEqual("", mirror.bundle_manifest_version(raw))

    def test_capture_limit_keeps_the_body_without_a_second_request(self):
        bundle = self._bundle('name = example\nversion = 3.2.1\n')
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.side_effect = [bundle, b""]
        with mock.patch.object(mirror, "urlopen", return_value=response) as opened:
            digest, size, _, _, body = mirror.sha256_stream(
                self.URL, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
        opened.assert_called_once()
        self.assertEqual(len(bundle), size)
        self.assertEqual(bundle, body)
        self.assertEqual("3.2.1", mirror.bundle_manifest_version(body))

    def test_oversized_downloads_are_hashed_without_being_buffered(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.side_effect = [b"x" * 64, b"y" * 64, b""]
        with mock.patch.object(mirror, "urlopen", return_value=response):
            _, size, _, _, body = mirror.sha256_stream(self.URL, capture_limit=100)
        self.assertEqual(128, size)
        self.assertIsNone(body)

    def test_recovered_version_is_cached_so_it_costs_one_download(self):
        bundle = self._bundle('name = example\nversion = 3.2.1\n')
        entry = {"download_url": self.URL, "version": "unknown"}
        with mock.patch.object(
            mirror, "sha256_stream",
            return_value=("a" * 64, len(bundle), None, None, bundle),
        ) as download:
            record, error = mirror.cached_download(
                entry, now=100, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
            self.assertIsNone(error)
            self.assertEqual("3.2.1", record["manifest_version"])
            again, _ = mirror.cached_download(
                entry, record, now=200, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
            self.assertEqual("3.2.1", again["manifest_version"])
            download.assert_called_once()

    def test_a_bundle_declaring_no_version_is_not_re_fetched_every_build(self):
        bundle = self._bundle("name = example\n")
        entry = {"download_url": self.URL, "version": "unknown"}
        with mock.patch.object(
            mirror, "sha256_stream",
            return_value=("a" * 64, len(bundle), None, None, bundle),
        ) as download:
            record, _ = mirror.cached_download(
                entry, now=100, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
            self.assertEqual("", record["manifest_version"])
            mirror.cached_download(
                entry, record, now=200, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
            download.assert_called_once()

    def test_a_cache_without_a_manifest_version_is_inspected_unconditionally(self):
        """Records written before this existed hold no answer to reuse."""
        bundle = self._bundle('name = example\nversion = 3.2.1\n')
        entry = {"download_url": self.URL, "version": "unknown"}
        legacy = {"sha256": "a" * 64, "size": 5, "version": "unknown",
                  "etag": '"old"', "next_check": 10 ** 9}
        with mock.patch.object(
            mirror, "sha256_stream",
            return_value=("a" * 64, len(bundle), None, None, bundle),
        ) as download:
            record, _ = mirror.cached_download(
                entry, legacy, now=100, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
        download.assert_called_once_with(
            self.URL, validators=None, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
        )
        self.assertEqual("3.2.1", record["manifest_version"])

    def test_a_failing_url_keeps_its_backoff_instead_of_being_re_probed(self):
        entry = {"download_url": self.URL, "version": "unknown"}
        with mock.patch.object(
            mirror, "sha256_stream", side_effect=URLError("offline"),
        ) as download:
            record, error = mirror.cached_download(
                entry, now=100, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
            self.assertIsNotNone(error)
            mirror.cached_download(
                entry, record, now=200, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
            download.assert_called_once()
