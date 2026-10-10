"""History traversal, ordering, retained notes and native keyboard regression checks."""
import json
import types
import unittest
import weakref
from urllib.error import HTTPError
from unittest import mock

from test_changelogs import changelogs, Model


def response(releases, more=False, headers=None):
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = json.dumps(releases, ensure_ascii=False).encode("utf-8")
    result.headers = dict(headers or {})
    if more:
        result.headers["Link"] = '<https://api.github.com/next>; rel="next"'
    return result


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.model = Model(addonVersionName="2.0", sourceURL="https://github.com/owner/project", homepage=None)

    def test_one_page_per_action_exposes_exact_continuation_and_reuses_page_cache(self):
        releases = [{"id": n, "tag_name": str(n), "body": "Caf\u00e9 \u0ba4\u0bae\u0bbf\u0bb4\u0bcd"}
                    for n in range(1, 106)]
        with mock.patch.object(changelogs, "urlopen", side_effect=[
            response(releases[:100], True), response(releases[100:]),
        ]) as opened:
            history = changelogs.ReleaseHistory()
            result, error = history.get("owner/project")
            older, olderError = history.get("owner/project", "github", result.nextPage)
            cachedOlder, cachedError = history.get("owner/project", "github", result.nextPage)
        self.assertIsNone(error)
        self.assertEqual(100, len(result))
        self.assertEqual(2, result.nextPage)
        self.assertEqual((older, olderError), (cachedOlder, cachedError))
        self.assertEqual(releases[-1]["body"], older[-1]["body"])
        self.assertEqual(2, opened.call_count)
        self.assertTrue(opened.call_args_list[0].args[0].full_url.endswith("per_page=100&page=1"))
        self.assertTrue(opened.call_args_list[1].args[0].full_url.endswith("per_page=100&page=2"))

    def test_rate_limit_on_explicit_continuation_keeps_first_page_cached(self):
        history = changelogs.ReleaseHistory()
        with mock.patch.object(changelogs, "urlopen", side_effect=[
            response([{"id": 1, "tag_name": "1.0", "body": "Earlier notes"}], True),
            HTTPError("x", 429, "", {"Retry-After": "60"}, None),
        ]):
            first, error = history.get("owner/project")
            _older, olderError = history.get("owner/project", "github", first.nextPage)
            rows = changelogs._historyRows(self.model, first, error)
            cached, cachedError = history.get("owner/project")
        self.assertEqual(first, cached)
        self.assertIsNone(cachedError)
        self.assertEqual("rateLimit", olderError)
        self.assertEqual([("1.0", "Earlier notes", "GitHub release")], rows)
        self.assertIsNone(rows.error)
        self.assertEqual(2, rows.nextPage)

    def test_link_continuation_is_not_reported_as_an_error(self):
        with mock.patch.object(
            changelogs, "urlopen", return_value=response([{"tag_name": "1.0", "body": "Kept"}], True),
        ):
            rows = changelogs.historyForModel(self.model, changelogs.ReleaseHistory())
        self.assertEqual("Kept", rows[0][1])
        self.assertIsNone(rows.error)
        self.assertEqual(2, rows.nextPage)

    def test_oversize_and_time_limits_are_reported(self):
        with mock.patch.object(changelogs, "MAX_RESPONSE_BYTES", 2), mock.patch.object(
            changelogs, "urlopen", return_value=response([{"body": "Too large"}]),
        ):
            self.assertEqual(([], "historyLimit"), changelogs.ReleaseHistory().get("owner/project"))
    def test_malformed_page_is_explicit_and_draft_and_duplicate_records_are_removed(self):
        release = {"id": 1, "tag_name": "1.0", "body": "Kept", "assets": ["not retained"]}
        with mock.patch.object(changelogs, "urlopen", return_value=response([
            release, release, {"id": 2, "draft": True},
        ])):
            result, error = changelogs.ReleaseHistory().get("owner/project")
        self.assertIsNone(error)
        self.assertEqual(1, len(result))
        self.assertNotIn("assets", result[0])
        with mock.patch.object(changelogs, "urlopen", return_value=response({"message": "bad response"})):
            self.assertEqual(([], "networkError"), changelogs.ReleaseHistory().get("other/project"))

    def test_cache_no_longer_truncates_injected_history(self):
        releases = [{"tag_name": str(n), "body": "Notes"} for n in range(150)]
        self.assertEqual((releases, None), changelogs.ReleaseHistory(fetch=lambda _: releases).get("o/r"))

    def test_publication_dates_override_api_order_and_unknown_dates_follow(self):
        rows = changelogs._historyRows(self.model, [
            {"tag_name": "v1.0", "body": "Old", "published_at": "2024-01-01T00:00:00Z"},
            {"tag_name": "v10.0", "body": "Unknown", "published_at": "invalid"},
            {"tag_name": "v2.0", "body": "New", "published_at": "2025-01-01T00:00:00Z"},
            {"tag_name": "v3.0", "body": "Unknown too", "created_at": "2026-01-01T00:00:00Z"},
            {"tag_name": "v4.0", "body": "  "},
        ], None)
        self.assertEqual(["v2.0", "v1.0", "v10.0", "v4.0", "v3.0"], [row[0] for row in rows])
        self.assertEqual("No release notes were published for this release.", rows[3][1])

    def test_matching_catalog_and_release_notes_are_merged_without_loss(self):
        setattr(self.model, changelogs.MODEL_CHANGELOG_ATTRIBUTE, "Catalog correction")
        rows = changelogs._historyRows(self.model, [
            {"tag_name": "v2.0", "body": "Author details", "published_at": "2025-01-01T00:00:00Z"},
            {"tag_name": "1.0", "body": "Old"},
        ], None)
        self.assertEqual(2, len(rows))
        self.assertEqual("2.0", rows[0][0])
        self.assertIn("Catalog correction", rows[0][1])
        self.assertIn("Author details", rows[0][1])
        self.assertEqual("catalog; GitHub release", rows[0][2])

    def test_embedded_explicit_version_sections_are_retrievable_without_github(self):
        self.model.manifest = {"changelog": "2.0:\nNew notes\n\n1.0 (earlier):\nOld notes"}
        rows = changelogs._historyRows(self.model, [], "notGitHub")
        self.assertEqual([("2.0", "New notes", "catalog; notGitHub"),
                          ("1.0", "Old notes", "catalog; notGitHub")], rows)
        self.assertEqual([("2.0", "Text\n1.0:\nJust one heading")],
                         changelogs._catalogHistory("2.0", "Text\n1.0:\nJust one heading"))

    def test_partial_refresh_retains_larger_previous_history_but_reports_failure(self):
        releases = [{"tag_name": "2.0", "body": "New"}, {"tag_name": "1.0", "body": "Old"}]
        now = [0]
        def fetch(_repository):
            if now[0]:
                raise changelogs.HistoryFetchError(releases[:1], "rateLimit")
            return releases
        history = changelogs.ReleaseHistory(fetch=fetch, now=lambda: now[0])
        self.assertEqual((releases, None), history.get("o/r"))
        now[0] = changelogs.CACHE_SECONDS + 1
        self.assertEqual((releases, "rateLimit"), history.get("o/r"))

    def test_longer_partial_refresh_merges_new_and_cached_older_records(self):
        original = [{"id": 1, "tag_name": "1.0", "body": "Old"}]
        updated = [{"id": 3, "tag_name": "3.0", "body": "Newest"},
                   {"id": 2, "tag_name": "2.0", "body": "New"}]
        now = [0]
        def fetch(_repository):
            if now[0]:
                raise changelogs.HistoryFetchError(updated, "networkError")
            return original
        history = changelogs.ReleaseHistory(fetch=fetch, now=lambda: now[0])
        history.get("o/r")
        now[0] = changelogs.CACHE_SECONDS + 1
        result, error = history.get("o/r")
        self.assertEqual(updated + original, result)
        self.assertEqual("networkError", error)
        rows = changelogs._historyRows(self.model, result, error)
        self.assertTrue(rows.usedCached)
        self.assertIn("Cached GitHub history retained", changelogs._historyStatus(rows))

    def test_numeric_aliases_merge_and_prereleases_follow_final_unknown_date(self):
        self.assertEqual(changelogs._versionIdentity("v2026.05.03"),
                         changelogs._versionIdentity("2026.5.3"))
        self.assertEqual(changelogs._versionIdentity("1.0"), changelogs._versionIdentity("1.0.0"))
        rows = changelogs._historyRows(self.model, [
            {"tag_name": "1.0-beta", "body": "Preview"},
            {"tag_name": "v1.0.0", "body": "Final"},
            {"tag_name": "1.0", "body": "Same version"},
        ], None)
        self.assertEqual(["v1.0.0", "1.0-beta"], [row[0] for row in rows])

    def test_catalog_preamble_duplicate_headings_crlf_and_development_versions_retain_text(self):
        self.model.manifest = {"changelog": "Introduction\r\n2.0:\r\nFirst\r\n2.0:\r\nSecond\r\n"
                                           "1.5.0-dev3 (candidate):\r\nDevelopment"}
        rows = changelogs._historyRows(self.model, [], "notGitHub")
        self.assertEqual(["2.0", "1.5.0-dev3"], [row[0] for row in rows])
        for text in ("Introduction", "First", "Second"):
            self.assertIn(text, rows[0][1])
        self.assertEqual("Development", rows[1][1])

    def test_status_counts_missing_notes_and_labels_dates_without_guessing_from_submission(self):
        self.model.submissionTime = 9999999999999
        setattr(self.model, changelogs.MODEL_CHANGELOG_ATTRIBUTE, "Catalog")
        rows = changelogs._historyRows(self.model, [
            {"tag_name": "1.0", "body": "Old", "published_at": "2024-01-02T03:00:00Z"},
            {"tag_name": "0.5", "body": " "},
        ], None)
        self.assertIn("2024-01-02 (UTC)", changelogs._historyItemLabel(rows, 0))
        self.assertIn("Release date unknown", changelogs._historyItemLabel(rows, 1))
        self.assertIn("1 GitHub published releases have notes", changelogs._historyStatus(rows))
        self.assertIn("1 have no published notes", changelogs._historyStatus(rows))
        self.assertIn("GitHub published releases", changelogs._historyStatus(rows))

    def test_commit_history_labels_unknown_and_known_dates_as_commit_dates(self):
        rows = changelogs._historyRows(self.model, [
            {"sha": "a" * 40, "message": "Unknown", "date": None},
            {"sha": "b" * 40, "message": "Invalid", "date": "not-a-date"},
        ], None, "commits")
        self.assertIn("Commit date unknown", changelogs._historyItemLabel(rows, 0))
        self.assertIn("Commit date unknown", changelogs._historyItemLabel(rows, 1))

        known = changelogs._historyRows(self.model, [
            {"sha": "c" * 40, "message": "Known", "date": "2024-01-02T03:00:00Z"},
        ], None, "commits")
        self.assertIn("2024-01-02 (UTC)", changelogs._historyItemLabel(known, 0))

    def test_commits_are_one_page_and_labeled_as_development_history(self):
        commits = [
            {"sha": "abcdef012345", "commit": {"message": "First subject\n\nDetails",
             "committer": {"date": "2026-01-02T03:04:05Z"}}},
            {"sha": "123456789abc", "commit": {"message": "Older", "author": {"date": "2025-01-01T00:00:00Z"}}},
        ]
        with mock.patch.object(changelogs, "urlopen", side_effect=[
            response(commits[:1], True), response(commits[1:]),
        ]) as opened:
            rows = changelogs.historyForModel(self.model, changelogs.ReleaseHistory(), "commits")
        self.assertEqual(1, len(rows))
        self.assertIn("Commit abcdef0: First subject", rows[0][0])
        self.assertEqual("First subject\n\nDetails", rows[0][1])
        self.assertEqual("GitHub commit", rows[0][2])
        self.assertIn("GitHub commit (development history)", changelogs._historyItemLabel(rows, 0))
        self.assertIn("commits, not published releases", changelogs._historyStatus(rows))
        self.assertIn("/commits?per_page=100&page=1", opened.call_args_list[0].args[0].full_url)
        self.assertEqual(1, opened.call_count)
        self.assertEqual(2, rows.nextPage)

    def test_bounded_history_exposes_explicit_continuation_page(self):
        page1 = [{"sha": "a" * 40, "commit": {"message": "New", "committer": {"date": None}}}]
        page2 = [{"sha": "b" * 40, "commit": {"message": "Old", "committer": {"date": None}}}]
        history = changelogs.ReleaseHistory()
        with mock.patch.object(changelogs, "urlopen", side_effect=[response(page1, True), response(page2)]) as opened:
            first, firstError = history.get("owner/project", "commits")
            second, secondError = history.get("owner/project", "commits", first.nextPage)
            history.get("owner/project", "commits", first.nextPage)
        self.assertIsNone(firstError)
        self.assertEqual(2, first.nextPage)
        self.assertEqual(["a" * 40], [record["sha"] for record in first])
        self.assertIsNone(secondError)
        self.assertEqual(["b" * 40], [record["sha"] for record in second])
        self.assertIn("page=2", opened.call_args_list[1].args[0].full_url)
        self.assertEqual(2, opened.call_count)

    def test_commit_source_never_falls_back_to_catalog_notes(self):
        setattr(self.model, changelogs.MODEL_CHANGELOG_ATTRIBUTE, "Catalog-only text")
        with mock.patch.object(changelogs, "urlopen", return_value=response([])):
            rows = changelogs.historyForModel(self.model, changelogs.ReleaseHistory(), "commits")
        self.assertNotIn("Catalog-only text", rows[0][1])
        self.assertEqual("commits", rows.sourcePreference)

    def test_remaining_zero_starts_shared_cooldown_and_retry_resumes_after_reset(self):
        now = [1000.0]
        history = changelogs.ReleaseHistory(now=lambda: now[0])
        with mock.patch.object(changelogs, "urlopen", side_effect=[
            response([{"id": 1, "tag_name": "1.0", "body": "Notes"}], headers={
                "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100",
            }),
            response([{"sha": "a" * 40, "commit": {"message": "After reset"}}]),
        ]) as opened:
            releases, releaseError = history.get("owner/project")
            blocked, blockedError = history.get("owner/project", "commits")
            now[0] = 1101.0
            commits, commitError = history.get("owner/project", "commits")
        self.assertEqual(1, len(releases))
        self.assertIsNone(releaseError)
        self.assertEqual(([], "rateLimit"), (blocked, blockedError))
        self.assertEqual("a" * 40, commits[0]["sha"])
        self.assertIsNone(commitError)
        self.assertEqual(2, opened.call_count)

    def test_rate_limit_with_stale_reset_uses_bounded_fallback(self):
        now = [5000.0]
        history = changelogs.ReleaseHistory(now=lambda: now[0])
        error = HTTPError("x", 429, "limited", {"X-RateLimit-Reset": "1"}, None)
        with mock.patch.object(changelogs, "urlopen", side_effect=error):
            self.assertEqual(([], "rateLimit"), history.get("owner/project"))
        self.assertEqual(now[0] + changelogs.RATE_LIMIT_FALLBACK_SECONDS, history._cooldownUntil)

    def test_remaining_zero_uses_later_primary_reset_than_retry_after(self):
        now = [1000.0]
        history = changelogs.ReleaseHistory(now=lambda: now[0])
        history._noteRateLimit({
            "X-RateLimit-Remaining": "0", "Retry-After": "60",
            "X-RateLimit-Reset": "4600",
        })
        self.assertEqual(4600.0, history._cooldownUntil)

    def test_remaining_zero_header_starts_cooldown_before_oversize_body_failure(self):
        now = [1000.0]
        history = changelogs.ReleaseHistory(now=lambda: now[0])
        oversized = response([], headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100",
        })
        oversized.read.return_value = b"xxx"
        with mock.patch.object(changelogs, "MAX_RESPONSE_BYTES", 2), mock.patch.object(
            changelogs, "urlopen", return_value=oversized,
        ) as opened:
            self.assertEqual(([], "historyLimit"), history.get("owner/project"))
            self.assertEqual(([], "rateLimit"), history.get("owner/project", "commits"))
        self.assertEqual(1, opened.call_count)

    def test_cache_retention_is_false_when_refresh_contains_all_cached_records(self):
        records = [{"id": 1, "body": "Notes"}]
        merged = changelogs._mergeCachedRecords(records, records)
        self.assertEqual(records, merged)
        self.assertFalse(merged.usedCached)

    def test_retained_repository_memory_limit_preserves_records_and_reports_incomplete(self):
        records = [{"id": 1, "tag_name": "2.0", "body": "A" * 100},
                   {"id": 2, "tag_name": "1.0", "body": "B" * 100}]
        one_record_size = changelogs._recordsBytes(changelogs.ReleaseRecords(records[:1])) + 16
        with mock.patch.object(changelogs, "MAX_RETAINED_REPOSITORY_BYTES", one_record_size):
            history = changelogs.ReleaseHistory(fetch=lambda _: records)
            result, error = history.get("o/r")
        self.assertEqual(records[:1], result)
        self.assertEqual("historyLimit", error)

    def test_aggregate_cache_memory_budget_evicts_old_entries(self):
        records = [{"id": 1, "tag_name": "1.0", "body": "A" * 1000}]
        weight = changelogs._recordsBytes(changelogs.ReleaseRecords(records))
        with mock.patch.object(changelogs, "MAX_CACHED_BYTES", weight * 2), mock.patch.object(
            changelogs, "MAX_RETAINED_REPOSITORY_BYTES", weight,
        ):
            history = changelogs.ReleaseHistory(fetch=lambda _: records)
            for index in range(10):
                history.get("owner/repo%d" % index)
                self.assertLessEqual(sum(history._cacheBytes.values()), weight * 2)
            self.assertLessEqual(len(history._cache), 2)
            self.assertIn("owner/repo9", history._cache)
            self.assertNotIn("owner/repo0", history._cache)


class ViewerTests(unittest.TestCase):
    def test_tab_reaches_notes_and_escape_returns_to_selected_row(self):
        wx = types.ModuleType("wx")
        for name in ("VERTICAL", "HORIZONTAL", "ALL", "LEFT", "RIGHT", "EXPAND", "TE_MULTILINE",
                     "TE_READONLY", "TE_RICH2", "ID_CANCEL", "WXK_RETURN", "WXK_ESCAPE"):
            setattr(wx, name, 1 << len(wx.__dict__))
        for name in ("EVT_LISTBOX", "EVT_LISTBOX_DCLICK", "EVT_CHAR_HOOK", "EVT_BUTTON"):
            setattr(wx, name, object())
        wx.NOT_FOUND = -1
        focus = [None]
        controls = []
        class Control:
            def __init__(self, *_args, **kwargs):
                self.handlers = {}; self.selection = -1; self.value = ""; self.options = kwargs
                controls.append(self)
            def Bind(self, event, handler): self.handlers[event] = handler
            def SetFocus(self): focus[0] = self
            def GetSelection(self): return self.selection
            def SetSelection(self, selection): self.selection = selection
            def ChangeValue(self, value): self.value = value
            def SetInsertionPoint(self, point): self.point = point
            def EndModal(self, code): self.closed = code
            def Destroy(self): self.destroyed = True
            def SetSizerAndFit(self, _sizer): pass
            def SetSize(self, _size): pass
            def ShowModal(dialog):
                choice = next(c for c in controls if "choices" in c.options)
                text = next(c for c in controls if c.options.get("style", 0) & wx.TE_RICH2)
                status = next(c for c in controls if "style" in c.options and c is not text)
                self.assertTrue(status.options["style"] & wx.TE_READONLY)
                self.assertIn("versions with available notes", status.value)
                self.assertTrue(text.options["style"] & wx.TE_READONLY)
                # The natural tab sequence is list, notes, status, then Close.
                self.assertLess(controls.index(text), controls.index(status))
                self.assertIs(choice, focus[0])
                choice.SetSelection(1)
                choice.handlers[wx.EVT_LISTBOX](None)
                self.assertIs(choice, focus[0])
                self.assertEqual("Older notes", text.value)
                def key(code):
                    event = types.SimpleNamespace(GetKeyCode=lambda: code, Skip=mock.Mock())
                    dialog.handlers[wx.EVT_CHAR_HOOK](event)
                    return event
                key(wx.WXK_RETURN)
                self.assertIs(choice, focus[0])
                self.assertTrue(key(wx.WXK_RETURN).Skip.called)
                # wx moves focus to the next native control when Tab is pressed.
                text.SetFocus()
                key(wx.WXK_ESCAPE)
                self.assertIs(choice, focus[0])
                self.assertEqual(1, choice.GetSelection())
                self.assertTrue(key(0).Skip.called)
                key(wx.WXK_ESCAPE)
                self.assertEqual(wx.ID_CANCEL, dialog.closed)
        wx.Dialog = wx.ListBox = wx.TextCtrl = wx.StaticText = wx.Button = Control
        wx.Window = types.SimpleNamespace(FindFocus=lambda: focus[0])
        wx.BoxSizer = lambda *_args: types.SimpleNamespace(Add=lambda *_a: None)
        parent = Model(_serrebiChangelogAlive=True)
        feature = changelogs.ChangelogFeature(None)
        rows = changelogs.HistoryRows([("2.0", "New notes", "catalog"), ("1.0", "Older notes", "GitHub release")])
        with mock.patch.dict("sys.modules", {"wx": wx}), mock.patch.object(
            changelogs, "isSecureDesktop", return_value=False,
        ):
            feature._showDialog(Model(), "Example", rows, "github", 0, 0, weakref.ref(parent))
        self.assertTrue(controls[0].destroyed)

    def test_destroyed_parent_and_obsolete_requests_cannot_open_a_viewer(self):
        feature = changelogs.ChangelogFeature(None)
        with mock.patch.dict("sys.modules", {"wx": types.ModuleType("wx")}), mock.patch.object(
            changelogs, "isSecureDesktop", return_value=False,
        ):
            parent = Model(_serrebiChangelogAlive=False)
            feature._showDialog(Model(), "Example", [], "github", 0, 0, weakref.ref(parent))
            parent._serrebiChangelogAlive = True
            feature._showDialog(Model(), "Example", [], "github", 1, 0, weakref.ref(parent))
            feature._showDialog(Model(), "Example", [], "github", 0, 1, weakref.ref(parent))
