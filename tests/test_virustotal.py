import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mirror  # noqa: E402

STATS = {"malicious": 0, "suspicious": 0, "undetected": 60, "harmless": 0}


def entry(source, version, sha="a" * 64, url="https://host.invalid/x.nvda-addon"):
    return {"name": "demo", "channel": "stable", "source": source,
            "version": version, "download_url": url, "sha256": sha}


class NewestReleaseWinsTests(unittest.TestCase):
    def test_a_newer_bestmidi_release_beats_an_older_store_copy(self):
        [winner] = mirror.dedupe([entry("official", "1.0"), entry("bestmidi", "2.0")])
        self.assertEqual("bestmidi", winner["source"])

    def test_a_tie_still_goes_to_the_stronger_source(self):
        [winner] = mirror.dedupe([entry("bestmidi", "2.0"), entry("official", "2.0")])
        self.assertEqual("official", winner["source"])

    def test_a_pin_wins_over_any_version(self):
        [winner] = mirror.dedupe([entry("bestmidi", "9.0"), entry("pinned", "1.0")])
        self.assertEqual("pinned", winner["source"])


class VirusTotalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = os.path.join(directory.name, "vt.json")
        self.calls = []
        self.answers = {}
        sleep = mock.patch.object(mirror.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def fake(self, path, key, data=None):
        self.calls.append(path)
        return self.answers.get(path, (404, None))

    def scan(self, entries, now=1000.0, key="k"):
        with mock.patch.object(mirror, "_vt_request", side_effect=self.fake):
            return mirror.virustotal_scans(entries, now=now, cache_path=self.cache, key=key)

    def test_a_known_file_gets_its_scan_without_any_upload(self):
        self.answers["files/" + "a" * 64] = (
            200, {"data": {"attributes": {"last_analysis_stats": STATS}}})
        e = entry("bestmidi", "1.0")
        self.assertEqual([], self.scan([e]))
        self.assertEqual(["files/" + "a" * 64], self.calls)
        self.assertEqual(STATS, e["scan_results"]["virusTotal"][0]["last_analysis_stats"])

    def test_an_unknown_file_is_submitted_by_url_then_rechecked_later(self):
        e = entry("bestmidi", "1.0")
        self.answers["urls"] = (200, {})
        self.scan([e])
        self.assertEqual(["files/" + "a" * 64, "urls"], self.calls)
        self.assertNotIn("scan_results", e)
        # Inside the recheck window nothing is asked.
        self.calls.clear()
        self.scan([entry("bestmidi", "1.0")], now=1000.0 + 60)
        self.assertEqual([], self.calls)
        # After it, the verdict is read and nothing is resubmitted.
        self.answers["files/" + "a" * 64] = (
            200, {"data": {"attributes": {"last_analysis_stats": STATS}}})
        e = entry("bestmidi", "1.0")
        self.scan([e], now=1000.0 + mirror.VT_PENDING_RECHECK_SECONDS)
        self.assertEqual(["files/" + "a" * 64], self.calls)
        self.assertIn("scan_results", e)

    def test_flagged_files_are_returned_and_cached_answers_need_no_key(self):
        flagged = dict(STATS, malicious=mirror.VT_MALICIOUS_BLOCK)
        self.answers["files/" + "a" * 64] = (
            200, {"data": {"attributes": {"last_analysis_stats": flagged}}})
        self.assertEqual(1, len(self.scan([entry("bestmidi", "1.0")])))
        self.calls.clear()
        self.assertEqual(1, len(self.scan([entry("bestmidi", "1.0")], key="")))
        self.assertEqual([], self.calls)

    def test_store_scanned_files_are_left_alone_and_budget_holds(self):
        store = dict(entry("official", "1.0"), scan_results={"virusTotal": []})
        many = [entry("bestmidi", "1.0", sha=f"{i:064x}") for i in range(30)]
        with mock.patch.object(mirror, "VT_CALLS_PER_BUILD", 10):
            self.scan([store] + many)
        self.assertEqual(10, len(self.calls))
        with open(self.cache, encoding="utf-8") as f:
            self.assertEqual(10, json.load(f)["__budget__"]["used"])

    def test_a_spent_quota_stops_the_build_asking(self):
        self.answers["files/" + "a" * 64] = (429, None)
        self.scan([entry("bestmidi", "1.0"), entry("ru", "1.0", sha="b" * 64)])
        self.assertEqual(1, len(self.calls))


class PublishThenRemoveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target, value in (("VT_CACHE_PATH", os.path.join(directory.name, "vt.json")),):
            p = mock.patch.object(mirror, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(mirror.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        self.answer = (404, None)

    def gate(self, entries, previous=()):
        with mock.patch.dict(os.environ, {"VT_API_KEY": "k"}),                 mock.patch.object(mirror, "_vt_request",
                                  side_effect=lambda *a, **k: self.answer):
            return mirror.virustotal_gate(
                [(e, e["sha256"], 1) for e in entries], list(previous))

    def test_an_unscanned_file_publishes_at_once_without_invented_scan_data(self):
        e = entry("bestmidi", "2.0")
        kept, held = self.gate([e])
        self.assertEqual((1, []), (len(kept), held))
        self.assertNotIn("scan_results", e)

    def test_a_flagged_file_is_removed(self):
        self.answer = (200, {"data": {"attributes": {"last_analysis_stats":
                                                     dict(STATS, malicious=5)}}})
        kept, held = self.gate([entry("bestmidi", "2.0")])
        self.assertEqual([], kept)
        self.assertIn("5 engines", held[0]["reason"])

    def test_a_queued_all_zero_answer_is_not_a_scan(self):
        zeros = {k: 0 for k in STATS}
        self.answer = (200, {"data": {"attributes": {"last_analysis_stats": zeros}}})
        e = entry("bestmidi", "2.0")
        kept, _held = self.gate([e])
        self.assertEqual(1, len(kept))
        self.assertNotIn("scan_results", e)


class CatalogSizeTests(unittest.TestCase):
    def test_a_new_size_claim_rechecks_once(self):
        cached = {"sha256": "a" * 64, "size": 100, "etag": '"1"', "version": "1.0",
                  "next_check": 10 ** 12}
        e = dict(entry("bestmidi", "1.0"), catalog_size=200)
        with mock.patch.object(mirror, "sha256_stream",
                               return_value=("b" * 64, 200, '"2"', None, None)) as fetch:
            record, error = mirror.cached_download(e, cached, now=1000)
        self.assertEqual((1, "b" * 64, None), (fetch.call_count, record["sha256"], error))
        # A catalog stuck on a size the file does not have asks only once.
        stuck = dict(cached, size_claim_checked=200)
        with mock.patch.object(mirror, "sha256_stream") as fetch:
            mirror.cached_download(e, stuck, now=1000)
        self.assertEqual(0, fetch.call_count)


if __name__ == "__main__":
    unittest.main()
