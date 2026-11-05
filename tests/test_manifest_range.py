import contextlib
import http.server
import io
import random
import threading
import unittest
import zipfile
from email.message import Message
from unittest import mock
from urllib.error import HTTPError, URLError

import mirror

URL = "https://example.invalid/releases/addon.nvda-addon"
CDN_URL = "https://cdn.example.invalid/signed/addon.nvda-addon"
#: Far bigger than the tail and manifest blocks together, so a test can tell a
#: manifest read from a download by the bytes served.
PADDING = random.Random(1).randbytes(2 * 1024 * 1024)


def bundle(manifest='name = realName\nsummary = "Real Name"\nversion = 1.0\n'):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("data.bin", PADDING)
        archive.writestr("manifest.ini", manifest)
        archive.writestr("more.bin", PADDING)
    return out.getvalue()


class FakeResponse(io.BytesIO):
    def __init__(self, body, status, url, host):
        super().__init__(body)
        self.status = status
        self.final_url = url
        self.host = host

    def geturl(self):
        return self.final_url

    def read(self, *args):
        data = super().read(*args)
        self.host.served += len(data)
        return data


class FakeHost:
    """A host answering range requests, redirecting once like GitHub does."""

    def __init__(self, raw, honours_ranges=True):
        self.raw = raw
        self.honours_ranges = honours_ranges
        self.urls = []
        self.served = 0

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        spec = request.get_header("Range")
        if not spec or not self.honours_ranges:
            return FakeResponse(self.raw, 200, CDN_URL, self)
        first, last = spec.removeprefix("bytes=").split("-")
        return FakeResponse(
            self.raw[int(first):int(last) + 1], 206, CDN_URL, self,
        )


def not_modified(*_args, **_kwargs):
    raise HTTPError(URL, 304, "Not modified", Message(), io.BytesIO())


class RangeReadTests(unittest.TestCase):
    def test_reads_the_name_without_downloading_the_bundle(self):
        raw = bundle()
        host = FakeHost(raw)
        with mock.patch.object(mirror, "urlopen", host):
            text = mirror.read_manifest_by_range(URL, len(raw))
        self.assertEqual("realName", mirror._manifest_name(text))
        self.assertLess(host.served, len(raw) // 10)

    def test_only_the_first_request_touches_the_counted_url(self):
        raw = bundle()
        host = FakeHost(raw)
        with mock.patch.object(mirror, "urlopen", host):
            mirror.read_manifest_by_range(URL, len(raw))
        self.assertEqual(URL, host.urls[0])
        self.assertGreater(len(host.urls), 1)
        self.assertEqual({CDN_URL}, set(host.urls[1:]))

    def test_a_host_ignoring_ranges_is_never_read_from(self):
        raw = bundle()
        host = FakeHost(raw, honours_ranges=False)
        with mock.patch.object(mirror, "urlopen", host):
            text = mirror.read_manifest_by_range(URL, len(raw))
        self.assertEqual("", text)
        self.assertEqual(1, len(host.urls))
        self.assertEqual(0, host.served)

    def test_not_a_zip_yields_no_name(self):
        raw = b"not a zip" * 1000
        with mock.patch.object(mirror, "urlopen", FakeHost(raw)):
            self.assertEqual("", mirror.read_manifest_by_range(URL, len(raw)))

    def test_an_encrypted_manifest_yields_no_name_and_no_crash(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.ini", "name = realName\n")
        raw = bytearray(out.getvalue())
        raw[raw.rfind(b"PK\x01\x02") + 8] |= 1  # central directory: encrypted
        raw = bytes(raw)
        with mock.patch.object(mirror, "urlopen", FakeHost(raw)):
            self.assertEqual("", mirror.read_manifest_by_range(URL, len(raw)))

    def test_unknown_size_makes_no_request(self):
        with mock.patch.object(mirror, "urlopen") as opened:
            self.assertEqual("", mirror.read_manifest_by_range(URL, 0))
        opened.assert_not_called()


class CachedDownloadRangeTests(unittest.TestCase):
    """A 304 has no body, so an unnamed bundle is read by range at its recheck."""

    def setUp(self):
        self.raw = bundle()
        self.entry = {"download_url": URL, "version": "1.0"}
        self.cached = {"sha256": "a" * 64, "size": len(self.raw),
                       "version": "1.0", "etag": '"old"', "next_check": 200}

    def recheck(self, cached, host, now=201):
        with mock.patch.object(mirror, "sha256_stream", side_effect=not_modified), \
                mock.patch.object(mirror, "urlopen", host):
            return mirror.cached_download(
                self.entry, cached, now=now,
                capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )

    def test_304_fills_in_a_missing_name(self):
        record, error = self.recheck(self.cached, FakeHost(self.raw))
        self.assertIsNone(error)
        self.assertEqual("realName", record["manifest_name"])
        self.assertEqual("a" * 64, record["sha256"])

    def test_304_rereads_a_name_recorded_empty(self):
        # Early builds recorded "" on a 304, and an unreadable oversized body
        # still does: neither ever looked inside the bundle.
        record, _ = self.recheck(dict(self.cached, manifest_name=""),
                                 FakeHost(self.raw))
        self.assertEqual("realName", record["manifest_name"])

    def test_a_known_name_costs_no_range_request(self):
        host = FakeHost(self.raw)
        record, _ = self.recheck(
            dict(self.cached, manifest_name="known", manifest_last_tested="2026.1"),
            host,
        )
        self.assertEqual("known", record["manifest_name"])
        self.assertEqual([], host.urls)

    def test_a_known_name_still_gets_its_api_versions_once(self):
        raw = bundle('name = realName\nversion = 1.0\nlastTestedNVDAVersion = 2026.2\n')
        host = FakeHost(raw)
        cached = dict(self.cached, size=len(raw), manifest_name="known")
        record, _ = self.recheck(cached, host)
        self.assertEqual("2026.2", record["manifest_last_tested"])
        self.assertEqual("realName", record["manifest_name"])
        requests = len(host.urls)
        self.recheck(record, host, now=record["next_check"] + 1)
        self.assertEqual(requests, len(host.urls))

    def test_a_refusal_keeps_a_known_name(self):
        host = FakeHost(self.raw, honours_ranges=False)
        record, _ = self.recheck(dict(self.cached, manifest_name="known"), host)
        self.assertEqual("known", record["manifest_name"])
        self.assertIsNone(record["manifest_last_tested"])

    def test_a_refusing_host_is_asked_only_once(self):
        host = FakeHost(self.raw, honours_ranges=False)
        record, _ = self.recheck(self.cached, host)
        self.assertEqual(1, len(host.urls))
        again, _ = self.recheck(record, host, now=record["next_check"] + 1)
        self.assertEqual(1, len(host.urls))
        self.assertFalse(again["manifest_name"])

    def test_a_failed_read_is_retried_at_the_next_recheck(self):
        def unreachable(*_args, **_kwargs):
            raise URLError("timed out")

        record, error = self.recheck(self.cached, unreachable)
        self.assertIsNone(error)
        self.assertNotIn("manifest_name", record)
        again, _ = self.recheck(record, FakeHost(self.raw),
                                now=record["next_check"] + 1)
        self.assertEqual("realName", again["manifest_name"])

    def test_a_warm_cache_makes_no_range_request(self):
        host = FakeHost(self.raw)
        with mock.patch.object(mirror, "sha256_stream") as stream, \
                mock.patch.object(mirror, "urlopen", host):
            mirror.cached_download(
                self.entry, dict(self.cached, next_check=999), now=100,
                capture_limit=mirror.MANIFEST_CAPTURE_LIMIT,
            )
        stream.assert_not_called()
        self.assertEqual([], host.urls)

    def test_an_oversized_download_gets_its_name_by_range(self):
        # Vision Assistant Pro, 40 MB: over the capture limit, so hashing the
        # whole file still left the name unread.
        host = FakeHost(self.raw)
        with mock.patch.object(
            mirror, "sha256_stream",
            return_value=("b" * 64, len(self.raw), None, None, None),
        ), mock.patch.object(mirror, "urlopen", host):
            record, _ = mirror.cached_download(
                self.entry, now=100, capture_limit=len(self.raw) - 1,
            )
        self.assertEqual("realName", record["manifest_name"])


class RealHttpTests(unittest.TestCase):
    """The same read over real sockets: urllib, redirects and a real 206."""

    def serve(self, raw, honours_ranges=True):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Range")))
                if self.path == "/asset":
                    self.send_response(302)
                    self.send_header("Location", "/cdn")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                spec = self.headers.get("Range")
                if spec and honours_ranges:
                    first, last = spec.removeprefix("bytes=").split("-")
                    body = raw[int(first):int(last) + 1]
                    self.send_response(206)
                    self.send_header(
                        "Content-Range", f"bytes {first}-{last}/{len(raw)}")
                else:
                    body = raw
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                # The client is meant to hang up on a refused range.
                with contextlib.suppress(OSError):
                    self.wfile.write(body)

        class Quiet(http.server.ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def handle_error(self, request, client_address):
                pass

        server = Quiet(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/asset", requests

    def test_reads_through_a_redirect_and_stays_on_its_target(self):
        raw = bundle()
        url, requests = self.serve(raw)
        text = mirror.read_manifest_by_range(url, len(raw))
        self.assertEqual("realName", mirror._manifest_name(text))
        self.assertEqual(1, sum(1 for path, _ in requests if path == "/asset"))

    def test_a_host_ignoring_ranges_gets_one_request_and_no_read(self):
        raw = bundle()
        url, requests = self.serve(raw, honours_ranges=False)
        self.assertEqual("", mirror.read_manifest_by_range(url, len(raw)))
        self.assertEqual(["/asset", "/cdn"], [path for path, _ in requests])

    def test_a_dead_host_raises_for_a_retry(self):
        url, _ = self.serve(b"")
        dead = url.rsplit(":", 1)[0] + ":1/asset"
        with self.assertRaises(OSError):
            mirror.read_manifest_by_range(dead, 100, timeout=2)


class RenameKeepsTranslationsTests(unittest.TestCase):
    """translations.json is keyed by the published ID at the time of writing."""

    def test_a_translation_under_the_catalog_name_survives_the_rename(self):
        entry = {"name": "Nombre Viejo", "source": "bestmidi",
                 "download_url": URL, "summary": "Nombre Viejo"}
        mirror.apply_manifest_names([entry], {URL: {"manifest_name": "realName"}})
        self.assertEqual("realName", entry["name"])
        with mock.patch.object(
            mirror, "TRANSLATIONS", {"Nombre Viejo": {"summary": "Old Name"}},
        ):
            mirror._translate_entry(entry)
        self.assertEqual("Old Name", entry["summary"])

    def test_a_translation_under_the_manifest_name_wins(self):
        entry = {"name": "realName", "catalog_name": "Nombre Viejo",
                 "download_url": URL, "summary": "x"}
        with mock.patch.object(mirror, "TRANSLATIONS", {
            "Nombre Viejo": {"summary": "Old"}, "realName": {"summary": "New"},
        }):
            mirror._translate_entry(entry)
        self.assertEqual("New", entry["summary"])


if __name__ == "__main__":
    unittest.main()
