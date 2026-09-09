import io
import tempfile
import unittest
import zipfile
from email.message import Message
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
        error = HTTPError(
            self.entry["download_url"], 304, "Not modified", Message(), io.BytesIO(),
        )
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
    def _bundle(manifest, extra=None):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("manifest.ini", manifest)
            for name, content in (extra or {}).items():
                archive.writestr(name, content)
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
                inspect=True,
            )
        download.assert_called_once_with(
            self.URL, validators=None, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
        )
        self.assertEqual("3.2.1", record["manifest_version"])

    def test_capturing_bytes_alone_does_not_force_a_download(self):
        """Harvesting translations must never re-download the whole catalog.

        Every bundle is captured now, so if capture implied inspection the
        first build after that change would refetch every cached add-on at
        once. Only a version the catalogs failed to state earns a forced
        download; translations wait for the ordinary recheck schedule.
        """
        entry = {"download_url": self.URL, "version": "1.0"}
        fresh = {"sha256": "a" * 64, "size": 5, "version": "1.0",
                 "etag": '"old"', "next_check": 10 ** 9}
        with mock.patch.object(mirror, "sha256_stream") as download:
            record, error = mirror.cached_download(
                entry, fresh, now=100, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
        download.assert_not_called()
        self.assertIsNone(error)
        self.assertEqual("a" * 64, record["sha256"])

    def test_author_translations_are_read_from_the_downloaded_bytes(self):
        bundle = self._bundle(
            "name = example\nversion = 1.0\n",
            extra={
                "locale/fr/manifest.ini": (
                    'summary = "Horloge"\ndescription = """Une horloge."""\n'
                ),
                "locale/de/manifest.ini": 'summary = "Uhr"\n',
                # English is the source language, never a translation.
                "locale/en/manifest.ini": 'summary = "Clock"\n',
            },
        )
        entry = {"download_url": self.URL, "version": "1.0"}
        with mock.patch.object(
            mirror, "sha256_stream",
            return_value=("a" * 64, len(bundle), None, None, bundle),
        ):
            record, _ = mirror.cached_download(
                entry, now=100, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
        self.assertEqual(
            {
                "fr": {"displayName": "Horloge", "description": "Une horloge."},
                "de": {"displayName": "Uhr"},
            },
            record["manifest_locales"],
        )

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


class PolledSourceTests(unittest.TestCase):
    """The catalog sources that cannot be revalidated cheaply.

    The NV Access store answers a conditional request with the whole 9.5 MB
    body whenever its CDN last refilled, and nvda-addons.ru, nvda.es and
    bestmidi send no validators at all. Caching keeps an hourly build from
    downloading about 15 MB of unchanged catalog data from four other people's
    servers. The poll interval is what decouples the two.
    """

    def setUp(self):
        self.cache = {}
        self.entries = [{"name": "clock", "version": "1.0"}]

    def _fetch(self, entries=None):
        return mock.Mock(return_value=self.entries if entries is None else entries)

    def test_first_poll_fetches_and_records_the_entries(self):
        fetch = self._fetch()
        entries = mirror.polled_source("ru", self.cache, fetch, ttl=3600, now=100)
        self.assertEqual(self.entries, entries)
        fetch.assert_called_once()
        self.assertEqual(3700, self.cache["ru"]["next_poll"])

    def test_build_inside_the_interval_makes_no_request(self):
        fetch = self._fetch()
        mirror.polled_source("ru", self.cache, fetch, ttl=3600, now=100)
        entries = mirror.polled_source("ru", self.cache, fetch, ttl=3600, now=3699)
        self.assertEqual(self.entries, entries)
        fetch.assert_called_once()

    def test_expired_interval_polls_again(self):
        fetch = self._fetch()
        mirror.polled_source("ru", self.cache, fetch, ttl=3600, now=100)
        mirror.polled_source("ru", self.cache, fetch, ttl=3600, now=3700)
        self.assertEqual(2, fetch.call_count)

    def test_reused_entries_are_isolated_from_the_cache(self):
        # The build mutates entries in place (version recovery, dedupe), and a
        # reused catalog must not accumulate one build's edits into the next.
        mirror.polled_source("ru", self.cache, self._fetch(), ttl=3600, now=100)
        first = mirror.polled_source("ru", self.cache, self._fetch(), ttl=3600, now=200)
        first[0]["version"] = "9.9"
        second = mirror.polled_source("ru", self.cache, self._fetch(), ttl=3600, now=300)
        self.assertEqual("1.0", second[0]["version"])

    def test_a_failed_poll_reuses_the_last_good_catalog(self):
        mirror.polled_source("ru", self.cache, self._fetch(), ttl=3600, now=100)
        failing = mock.Mock(side_effect=RuntimeError("nvda-addons.ru is down"))
        entries = mirror.polled_source("ru", self.cache, failing, ttl=3600, now=4000)
        self.assertEqual(self.entries, entries)

    def test_a_failed_poll_backs_off_instead_of_retrying_every_build(self):
        mirror.polled_source("ru", self.cache, self._fetch(), ttl=3600, now=100)
        failing = mock.Mock(side_effect=RuntimeError("nvda-addons.ru is down"))
        mirror.polled_source("ru", self.cache, failing, ttl=3600, now=4000)
        mirror.polled_source("ru", self.cache, failing, ttl=3600, now=4001)
        failing.assert_called_once()
        self.assertEqual(
            4000 + mirror.SOURCE_RETRY_SECONDS, self.cache["ru"]["next_poll"],
        )

    def test_a_first_poll_that_fails_still_fails_the_build(self):
        failing = mock.Mock(side_effect=RuntimeError("nvda-addons.ru is down"))
        with self.assertRaises(RuntimeError):
            mirror.polled_source("ru", self.cache, failing, ttl=3600, now=100)

    def test_sources_are_polled_independently(self):
        ru, es = self._fetch(), self._fetch([{"name": "es-addon"}])
        mirror.polled_source("ru", self.cache, ru, ttl=3600, now=100)
        mirror.polled_source("es", self.cache, es, ttl=3600, now=100)
        self.assertEqual([{"name": "es-addon"}], self.cache["es"]["entries"])
        self.assertEqual(self.entries, self.cache["ru"]["entries"])

    def test_zero_ttl_polls_every_build(self):
        # What --no-head-check asks for: trust nothing that was cached.
        fetch = self._fetch()
        mirror.polled_source("ru", self.cache, fetch, ttl=0, now=100)
        mirror.polled_source("ru", self.cache, fetch, ttl=0, now=100)
        self.assertEqual(2, fetch.call_count)

    def test_a_corrupt_cache_entry_is_treated_as_no_cache(self):
        self.cache["ru"] = {"entries": "not a list", "next_poll": 1 << 40}
        fetch = self._fetch()
        entries = mirror.polled_source("ru", self.cache, fetch, ttl=3600, now=100)
        self.assertEqual(self.entries, entries)
        fetch.assert_called_once()
