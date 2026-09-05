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
        with mock.patch.object(mirror, "sha256_stream", return_value=("b" * 64, 123, '"new"', None)) as download:
            record, error = mirror.cached_download(entry, self.cached, now=100)
        download.assert_called_once_with(entry["download_url"], validators=None)
        self.assertEqual("b" * 64, record["sha256"])
        self.assertIsNone(error)

    def test_304_reuses_hash_and_renews_ttl(self):
        error = HTTPError(self.entry["download_url"], 304, "Not modified", {}, None)
        with mock.patch.object(mirror, "sha256_stream", side_effect=error) as download:
            record, failure = mirror.cached_download(self.entry, self.cached, now=201)
        download.assert_called_once_with(self.entry["download_url"], validators=self.cached)
        self.assertEqual(self.cached["sha256"], record["sha256"])
        self.assertEqual(201 + mirror.DOWNLOAD_RECHECK_SECONDS, record["next_check"])
        self.assertIsNone(failure)

    def test_host_without_validators_downloads_at_most_once_per_day(self):
        with mock.patch.object(mirror, "sha256_stream", return_value=("a" * 64, 123, None, None)) as download:
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
        with mock.patch.object(mirror, "sha256_stream", return_value=("b" * 64, 123, None, None)):
            record, error = mirror.cached_download(self.entry, cached, now=201)
        self.assertNotIn("error", record)
        self.assertIsNone(error)

    def test_force_bypasses_ttl_and_conditional_headers(self):
        with mock.patch.object(mirror, "sha256_stream", return_value=("b" * 64, 123, None, None)) as download:
            mirror.cached_download(self.entry, self.cached, force=True, now=100)
        download.assert_called_once_with(self.entry["download_url"], validators=None)

    def test_stream_uses_conditional_get_without_range(self):
        for validators, expected in (({"etag": '"old"'}, "If-none-match"),
                                     ({"last_modified": "yesterday"}, "If-modified-since")):
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {}
            response.read.side_effect = [b"test", b""]
            with mock.patch.object(mirror, "urlopen", return_value=response) as opened:
                _, size, _, _ = mirror.sha256_stream(self.entry["download_url"], validators=validators)
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
