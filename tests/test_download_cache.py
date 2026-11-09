import io
import tempfile
import unittest
import zipfile
from email.message import Message
from email.utils import formatdate
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
        with mock.patch.object(mirror, "sha256_stream", side_effect=URLError("offline")) as download:
            record, failure = mirror.cached_download(self.entry, None, now=201)
            self.assertIn("offline", failure)
            again, repeated = mirror.cached_download(self.entry, record, now=801)
            self.assertEqual(failure, repeated)
            self.assertEqual(record, again)
            download.assert_called_once()
            mirror.cached_download(self.entry, record, now=201 + mirror.DOWNLOAD_RETRY_SECONDS)
            self.assertEqual(2, download.call_count)

    def _http_error(self, status):
        return HTTPError(self.entry["download_url"], status, "x", Message(), io.BytesIO())

    def test_outage_keeps_publishing_the_verified_hash_with_backoff(self):
        # nvda.ru times out from some GitHub runners while the files are fine.
        # Rejecting on that dropped up to 214 working add-ons per build.
        for fault in (URLError("timed out"), TimeoutError("timed out"),
                      self._http_error(503), self._http_error(429)):
            with mock.patch.object(mirror, "sha256_stream", side_effect=fault) as download:
                record, failure = mirror.cached_download(self.entry, self.cached, now=201)
                self.assertIsNone(failure)
                self.assertEqual(self.cached["sha256"], record["sha256"])
                self.assertNotIn("error", record)
                self.assertEqual(201, record["failing_since"])
                again, repeated = mirror.cached_download(self.entry, record, now=801)
                self.assertIsNone(repeated)
                self.assertEqual(record, again)
                download.assert_called_once()
                mirror.cached_download(self.entry, record, now=201 + mirror.DOWNLOAD_RETRY_SECONDS)
                self.assertEqual(2, download.call_count)

    def test_missing_file_is_rejected_at_once(self):
        with mock.patch.object(mirror, "sha256_stream", side_effect=self._http_error(404)):
            record, failure = mirror.cached_download(self.entry, self.cached, now=201)
        self.assertIn("404", failure)
        self.assertIn("404", record["error"])

    def test_outage_past_the_grace_period_is_rejected(self):
        grace = mirror.DOWNLOAD_OUTAGE_GRACE_SECONDS
        cached = dict(self.cached, failing_since=201)
        with mock.patch.object(mirror, "sha256_stream", side_effect=URLError("timed out")):
            record, failure = mirror.cached_download(self.entry, cached, now=200 + grace)
            self.assertIsNone(failure)
            self.assertEqual(201, record["failing_since"])
            record, failure = mirror.cached_download(self.entry, cached, now=201 + grace)
        self.assertIn("timed out", failure)
        self.assertIn("timed out", record["error"])

    def test_recovery_ends_the_outage(self):
        cached = dict(self.cached, failing_since=150)
        with mock.patch.object(mirror, "sha256_stream", side_effect=self._http_error(304)):
            record, failure = mirror.cached_download(self.entry, cached, now=201)
        self.assertIsNone(failure)
        self.assertNotIn("failing_since", record)
        with mock.patch.object(mirror, "sha256_stream", return_value=("b" * 64, 123, None, None, None)):
            record, failure = mirror.cached_download(self.entry, cached, now=201)
        self.assertIsNone(failure)
        self.assertNotIn("failing_since", record)

    def test_force_does_not_trust_the_old_hash_during_an_outage(self):
        with mock.patch.object(mirror, "sha256_stream", side_effect=URLError("timed out")):
            _, failure = mirror.cached_download(self.entry, self.cached, force=True, now=100)
        self.assertIn("timed out", failure)

    def test_outage_backoff_holds_for_inspection_and_fresh_uploads(self):
        cached = dict(self.cached, failing_since=150, next_check=5000,
                      last_modified=formatdate(150, usegmt=True))
        with mock.patch.object(mirror, "sha256_stream") as download:
            _, failure = mirror.cached_download(
                self.entry, cached, now=201, capture_limit=1024, inspect=True,
            )
        download.assert_not_called()
        self.assertIsNone(failure)

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

    def test_pinned_lookup_survives_rate_limit_only_when_verified_once(self):
        def refuse(code):
            def fetch():
                error = HTTPError("https://api.github.com/x", code, "no", Message(), None)
                self.addCleanup(error.close)
                raise error
            return fetch

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(mirror, "PINNED_BUNDLE_CACHE_PATH", directory), \
                mock.patch.object(mirror, "PINNED_STATE_PATH",
                                  directory + "/state.json"):
            with self.assertRaises(HTTPError):
                mirror._pinned_cached("releases u", "o/r", refuse(403))
            self.assertEqual([1], mirror._pinned_cached("releases u", "o/r", lambda: [1]))
            for code in (403, 429, 502):
                self.assertEqual([1], mirror._pinned_cached("releases u", "o/r", refuse(code)))
            with self.assertRaises(HTTPError):
                mirror._pinned_cached("releases u", "o/r", refuse(404))
            self.assertEqual([1], mirror._pinned_cached(
                "releases u", "o/r",
                mock.Mock(side_effect=URLError("timed out"))))

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

    def test_manifest_name_is_read_from_the_download_being_hashed(self):
        bundle = self._bundle('name = "reloqCExperimental"\nversion = 1.0.1\n')
        self.assertEqual("reloqCExperimental", mirror.bundle_manifest_name(bundle))

    def test_unreadable_bundles_yield_no_manifest_name_instead_of_raising(self):
        for raw in (b"", None, b"not a zip", self._bundle("version = 1.0\n")):
            self.assertEqual("", mirror.bundle_manifest_name(raw))

    def test_manifest_name_is_cached_with_the_download(self):
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
            self.assertEqual("example", record["manifest_name"])
            again, _ = mirror.cached_download(
                entry, record, now=200, capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
            self.assertEqual("example", again["manifest_name"])
            download.assert_called_once()

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

    def test_slow_downloads_fail_past_the_deadline(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.side_effect = [b"x" * 64, b"y" * 64, b""]
        clock = [1000.0, 1000.0, 1000.0 + mirror.DOWNLOAD_MAX_SECONDS + 1]
        with mock.patch.object(mirror, "urlopen", return_value=response), \
                mock.patch.object(mirror.time, "monotonic", side_effect=clock):
            with self.assertRaises(TimeoutError):
                mirror.sha256_stream(self.URL)

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


class FreshUploadRecheckTests(unittest.TestCase):
    """An author replacing a bundle under the same name within hours.

    The 24h recheck would keep publishing the stale sha256 for a day, which
    NVDA rejects on install. A bundle first seen within
    FRESH_UPLOAD_SECONDS gets a conditional recheck every FRESH_RECHECK_SECONDS
    until its upload ages out.
    """

    def setUp(self):
        self.entry = {"download_url": "https://example.invalid/addon",
                      "version": "1.0"}
        self.now = 2_000_000

    def _fresh_record(self, checked_at, uploaded_at):
        return {"sha256": "a" * 64, "size": 123, "version": "1.0",
                "etag": '"fresh"', "last_modified": formatdate(uploaded_at, usegmt=True),
                "next_check": checked_at + mirror.DOWNLOAD_RECHECK_SECONDS}

    def test_fresh_upload_rechecks_after_an_hour_via_conditional_get(self):
        cached = self._fresh_record(self.now, self.now)
        not_modified = HTTPError(
            self.entry["download_url"], 304, "Not modified", Message(), io.BytesIO(),
        )
        with mock.patch.object(mirror, "sha256_stream", side_effect=not_modified) as download:
            record, error = mirror.cached_download(
                self.entry, cached, now=self.now + mirror.FRESH_RECHECK_SECONDS,
            )
        download.assert_called_once_with(
            self.entry["download_url"], validators=cached, capture_limit=0,
        )
        self.assertEqual("a" * 64, record["sha256"])
        self.assertEqual(
            self.now + mirror.FRESH_RECHECK_SECONDS + mirror.DOWNLOAD_RECHECK_SECONDS,
            record["next_check"],
        )
        self.assertIsNone(error)

    def test_old_upload_still_waits_the_full_24h(self):
        uploaded = self.now - mirror.FRESH_UPLOAD_SECONDS - 3600
        cached = self._fresh_record(self.now, uploaded)
        with mock.patch.object(mirror, "sha256_stream") as download:
            record, error = mirror.cached_download(
                self.entry, cached, now=self.now + mirror.FRESH_RECHECK_SECONDS,
            )
        download.assert_not_called()
        self.assertEqual(cached, record)
        self.assertIsNone(error)

    def test_replaced_file_gets_the_new_sha256(self):
        cached = self._fresh_record(self.now, self.now)
        new_body = ("b" * 64, 456, '"replacement"',
                    formatdate(self.now + 3600, usegmt=True), None)
        with mock.patch.object(mirror, "sha256_stream", return_value=new_body) as download:
            record, error = mirror.cached_download(
                self.entry, cached, now=self.now + mirror.FRESH_RECHECK_SECONDS,
            )
        download.assert_called_once()
        self.assertEqual("b" * 64, record["sha256"])
        self.assertNotIn("ignores_conditional", record)
        self.assertEqual(
            self.now + mirror.FRESH_RECHECK_SECONDS + mirror.DOWNLOAD_RECHECK_SECONDS,
            record["next_check"],
        )
        self.assertIsNone(error)

    def test_host_ignoring_conditionals_is_flagged_once(self):
        cached = self._fresh_record(self.now, self.now)
        same_body = ("a" * 64, 123, '"fresh"',
                     formatdate(self.now + 3600, usegmt=True), None)
        with mock.patch.object(mirror, "sha256_stream", return_value=same_body) as download:
            record, error = mirror.cached_download(
                self.entry, cached, now=self.now + mirror.FRESH_RECHECK_SECONDS,
            )
            self.assertTrue(record["ignores_conditional"])
            self.assertIsNone(error)
            # Flagged: the hourly recheck never applies again, so the record
            # is trusted until the ordinary 24h TTL expires.
            again, _ = mirror.cached_download(
                self.entry, record, now=self.now + 2 * mirror.FRESH_RECHECK_SECONDS,
            )
            self.assertTrue(again["ignores_conditional"])
        download.assert_called_once()

    def test_entries_without_validators_never_fast_recheck(self):
        cached = {"sha256": "a" * 64, "size": 123, "version": "1.0",
                  "next_check": self.now + mirror.DOWNLOAD_RECHECK_SECONDS}
        with mock.patch.object(mirror, "sha256_stream") as download:
            record, error = mirror.cached_download(
                self.entry, cached, now=self.now + mirror.FRESH_RECHECK_SECONDS,
            )
        download.assert_not_called()
        self.assertEqual(cached, record)
        self.assertIsNone(error)
