import json
import tempfile
import unittest
from unittest import mock

import translation_issue


def make_findings():
    return [
        {
            "addonId": "exampleAddon",
            "sourceURL": "https://example.org/addon",
            "fields": {
                "displayName": {
                    "reason": "non-Latin script",
                    "text": "Пример (примерное описание)",
                },
                "description": {
                    "reason": "non-Latin script",
                    "text": "Очень длинное описание примера. " * 40,
                },
            },
        },
        {
            "addonId": "anotherOne",
            "sourceURL": "",
            "fields": {
                "displayName": {
                    "reason": "non-English function words: para",
                    "text": "Complemento para algo",
                },
            },
        },
    ]


class RenderBodyTests(unittest.TestCase):
    def test_body_is_deterministic_and_lists_every_addon(self):
        findings = make_findings()
        first = translation_issue.render_body(
            findings, "owner/repo"
        )
        second = translation_issue.render_body(
            json.loads(json.dumps(findings)), "owner/repo"
        )
        self.assertEqual(first, second)
        self.assertIn("`exampleAddon` (https://example.org/addon)", first)
        self.assertIn("`anotherOne` (no source URL)", first)
        self.assertIn("2 add-ons", first)
        self.assertIn("owner/repo/actions/workflows/update.yml", first)

    def test_long_text_is_wrapped_in_a_fence_it_cannot_break(self):
        body = translation_issue.render_body(make_findings(), "owner/repo")
        self.assertIn("```", body)
        # A fence-lengthening body would embed a longer fence; both real
        # findings render inside plain fences, so assert the invariant
        # directly on a hostile payload instead.
        hostile = [{"addonId": "x", "sourceURL": "",
                    "fields": {"description": {"reason": "r", "text": "```\nhi\n```"}}}]
        hostile_body = translation_issue.render_body(hostile, "owner/repo")
        self.assertIn("````", hostile_body)
        self.assertIn("`description` [r]:", hostile_body)

    def test_singular_grammar_for_one_finding(self):
        body = translation_issue.render_body(make_findings()[:1], "owner/repo")
        self.assertIn("1 add-on still", body)


class TempFileTestCase(unittest.TestCase):
    def write_findings(self, findings):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(findings, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(__import__("os").remove, handle.name)
        return handle.name


class OpenIssueTests(TempFileTestCase):
    def test_creates_issue_with_label_when_none_open(self):
        calls = []

        def gh(args):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return 0, "[]"
            if args[:2] == ["issue", "create"]:
                return 0, "https://github.com/owner/repo/issues/7"
            return 0, ""

        path = self.write_findings(make_findings())
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            translation_issue.open_issue(path, gh=gh)
        create = next(c for c in calls if c[:2] == ["issue", "create"])
        self.assertIn("--label", create)
        self.assertIn("translation-gap", create)
        body = create[create.index("--body") + 1]
        self.assertIn("`exampleAddon`", body)

    def test_updates_existing_issue_only_when_body_changed(self):
        findings = make_findings()
        path = self.write_findings(findings)
        body = translation_issue.render_body(findings, "owner/repo")
        calls = []

        def gh(args):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps([{"number": 3, "title": translation_issue.ISSUE_TITLE}])
            if args[:2] == ["issue", "view"]:
                return 0, json.dumps({"body": body})
            return 0, ""

        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            translation_issue.open_issue(path, gh=gh)
        self.assertFalse(any(c[:2] == ["issue", "edit"] for c in calls))

        changed = self.write_findings(
            findings + [{"addonId": "newOne", "sourceURL": "",
                         "fields": {"displayName": {"reason": "r", "text": "Nouveau texte ici"}}}]
        )
        calls.clear()
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            translation_issue.open_issue(changed, gh=gh)
        edit = next(c for c in calls if c[:2] == ["issue", "edit"])
        self.assertIn("newOne", edit[edit.index("--body") + 1])

    def test_ignores_other_issues_with_similar_titles(self):
        calls = []

        def gh(args):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps(
                    [{"number": 9, "title": translation_issue.ISSUE_TITLE + " (old)"}]
                )
            if args[:2] == ["issue", "create"]:
                return 0, "https://github.com/owner/repo/issues/8"
            return 0, ""

        path = self.write_findings(make_findings())
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            translation_issue.open_issue(path, gh=gh)
        create = next(c for c in calls if c[:2] == ["issue", "create"])
        self.assertIsNotNone(create)


class CloseIssueTests(unittest.TestCase):
    def test_closes_open_issue_with_comment(self):
        calls = []

        def gh(args):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps([{"number": 3, "title": translation_issue.ISSUE_TITLE}])
            return 0, ""

        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            translation_issue.close_issue(gh=gh)
        close = next(c for c in calls if c[:2] == ["issue", "close"])
        self.assertIn("--comment", close)

    def test_noop_when_no_issue_open(self):
        def gh(args):
            self.assertEqual(args[:2], ["issue", "list"])
            return 0, "[]"

        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            translation_issue.close_issue(gh=gh)  # must not raise

    def test_failure_raises(self):
        def gh(args):
            if args[:2] == ["issue", "list"]:
                return 1, "boom"
            return 0, ""

        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            with self.assertRaisesRegex(RuntimeError, "gh issue list failed"):
                translation_issue.close_issue(gh=gh)


class SyncIssueTests(TempFileTestCase):
    def test_findings_open_the_issue(self):
        calls = []

        def gh(args):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return 0, "[]"
            if args[:2] == ["issue", "create"]:
                return 0, "https://github.com/owner/repo/issues/11"
            return 0, ""

        path = self.write_findings(make_findings())
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            translation_issue.sync_issue(path, gh=gh)
        self.assertTrue(any(c[:2] == ["issue", "create"] for c in calls))

    def test_no_findings_close_the_issue(self):
        calls = []

        def gh(args):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps(
                    [{"number": 5, "title": translation_issue.ISSUE_TITLE}]
                )
            return 0, ""

        path = self.write_findings([])
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            translation_issue.sync_issue(path, gh=gh)
        self.assertTrue(any(c[:2] == ["issue", "close"] for c in calls))

    def test_missing_findings_file_raises_rather_than_closing(self):
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            with self.assertRaises(FileNotFoundError):
                translation_issue.sync_issue("does-not-exist.json", gh=lambda a: (0, ""))


class MainTests(TempFileTestCase):
    def test_usage_error_on_bad_arguments(self):
        self.assertEqual(2, translation_issue.main([]))
        self.assertEqual(2, translation_issue.main(["frobnicate", "x.json"]))
        self.assertEqual(2, translation_issue.main(["open"]))

    def test_close_takes_no_path_argument(self):
        # The workflow invokes `close` with no file argument.
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            with mock.patch.object(
                translation_issue, "close_issue", return_value=None
            ) as close:
                self.assertEqual(0, translation_issue.main(["close"]))
        close.assert_called_once_with()

    def test_sync_routes_to_open_and_close(self):
        with_findings = self.write_findings(make_findings())
        without = self.write_findings([])
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}):
            with mock.patch.object(
                translation_issue, "open_issue", return_value=None
            ) as opener, mock.patch.object(
                translation_issue, "close_issue", return_value=None
            ) as closer:
                self.assertEqual(0, translation_issue.main(["sync", with_findings]))
                self.assertEqual(0, translation_issue.main(["sync", without]))
        opener.assert_called_once()
        self.assertEqual(with_findings, opener.call_args.args[0])
        closer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
