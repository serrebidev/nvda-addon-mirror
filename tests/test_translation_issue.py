import io
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


class RenderBodyLimitTests(unittest.TestCase):
    def test_body_stays_bounded(self):
        finding = make_findings()[0]
        findings = [dict(finding, addonId=f"addon{index}") for index in range(200)]
        body = translation_issue.render_body(findings, "owner/repo")
        self.assertLessEqual(len(body), 65536)
        self.assertIn("more, left out to fit", body)
        self.assertIn("addon0", body)
        self.assertTrue(body.endswith("</sub>"))


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
        self.assertIn("git.serrebiradio.com/owner/repo", first)

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
    def setUp(self):
        # sync_issue prints TRANSLATION-QUEUE lines, and CI runs these tests in
        # the same refresh log the Muse cloud check greps. Fixture add-ons
        # must never reach it as if they were real gaps.
        patcher = mock.patch("sys.stdout", io.StringIO())
        patcher.start()
        self.addCleanup(patcher.stop)

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

        def api(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/issues?state=open&type=issues&limit=50"):
                return 200, []
            if method == "GET" and path.endswith("/labels"):
                return 200, []
            if method == "POST" and path.endswith("/labels"):
                return 201, {"id": 4, "name": "translation-gap"}
            if method == "POST" and path.endswith("/issues"):
                return 201, {"html_url": "https://forgejo.invalid/owner/repo/issues/7"}
            return 200, {}

        path = self.write_findings(make_findings())
        translation_issue.open_issue(path, api=api, repository="owner/repo")
        create = next(c for c in calls if c[0] == "POST" and c[1].endswith("/issues"))
        self.assertEqual([4], create[2]["labels"])
        self.assertIn("`exampleAddon`", create[2]["body"])

    def test_updates_existing_issue_only_when_body_changed(self):
        findings = make_findings()
        path = self.write_findings(findings)
        body = translation_issue.render_body(findings, "owner/repo")
        calls = []

        def api(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/issues?state=open&type=issues&limit=50"):
                return 200, [{"number": 3, "title": translation_issue.ISSUE_TITLE}]
            if method == "GET" and path.endswith("/issues/3"):
                return 200, {"body": body}
            return 200, {}

        translation_issue.open_issue(path, api=api, repository="owner/repo")
        self.assertFalse(any(c[0] == "PATCH" for c in calls))

        changed = self.write_findings(
            findings + [{"addonId": "newOne", "sourceURL": "",
                         "fields": {"displayName": {"reason": "r", "text": "Nouveau texte ici"}}}]
        )
        calls.clear()
        translation_issue.open_issue(changed, api=api, repository="owner/repo")
        edit = next(c for c in calls if c[0] == "PATCH")
        self.assertIn("newOne", edit[2]["body"])

    def test_ignores_other_issues_with_similar_titles(self):
        calls = []

        def api(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/issues?state=open&type=issues&limit=50"):
                return 200, [
                    {"number": 9, "title": translation_issue.ISSUE_TITLE + " (old)"}
                ]
            if method == "GET" and path.endswith("/labels"):
                return 200, [{"id": 4, "name": "translation-gap"}]
            if method == "POST" and path.endswith("/issues"):
                return 201, {"html_url": "https://forgejo.invalid/owner/repo/issues/8"}
            return 200, {}

        path = self.write_findings(make_findings())
        translation_issue.open_issue(path, api=api, repository="owner/repo")
        create = next(c for c in calls if c[0] == "POST" and c[1].endswith("/issues"))
        self.assertIsNotNone(create)


class CloseIssueTests(unittest.TestCase):
    def test_closes_open_issue_with_comment(self):
        calls = []

        def api(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/issues?state=open&type=issues&limit=50"):
                return 200, [{"number": 3, "title": translation_issue.ISSUE_TITLE}]
            return 200, {}

        translation_issue.close_issue(api=api, repository="owner/repo")
        comment = next(c for c in calls if c[1].endswith("/issues/3/comments"))
        self.assertEqual("POST", comment[0])
        self.assertIn("closing automatically", comment[2]["body"])
        close = next(
            c for c in calls
            if c[0] == "PATCH" and c[1].endswith("/issues/3")
        )
        self.assertEqual("closed", close[2]["state"])

    def test_noop_when_no_issue_open(self):
        def api(method, path, payload=None):
            self.assertTrue(path.endswith("/issues?state=open&type=issues&limit=50"))
            return 200, []

        translation_issue.close_issue(api=api, repository="owner/repo")  # must not raise

    def test_failure_raises(self):
        def api(method, path, payload=None):
            if method == "GET":
                return 500, "boom"
            return 200, {}

        with self.assertRaisesRegex(RuntimeError, "Forgejo GET issues failed"):
            translation_issue.close_issue(api=api, repository="owner/repo")


class SyncIssueTests(TempFileTestCase):
    def test_findings_open_the_issue(self):
        calls = []

        def api(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/issues?state=open&type=issues&limit=50"):
                return 200, []
            if method == "GET" and path.endswith("/labels"):
                return 200, [{"id": 4, "name": "translation-gap"}]
            if method == "POST" and path.endswith("/issues"):
                return 201, {"html_url": "https://forgejo.invalid/owner/repo/issues/11"}
            return 200, {}

        path = self.write_findings(make_findings())
        translation_issue.sync_issue(path, api=api, repository="owner/repo")
        self.assertTrue(any(c[0] == "POST" and c[1].endswith("/issues") for c in calls))

    def test_no_findings_close_the_issue(self):
        calls = []

        def api(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/issues?state=open&type=issues&limit=50"):
                return 200, [
                    {"number": 5, "title": translation_issue.ISSUE_TITLE}
                ]
            return 200, {}

        path = self.write_findings([])
        translation_issue.sync_issue(path, api=api, repository="owner/repo")
        self.assertTrue(
            any(c[0] == "PATCH" and c[1].endswith("/issues/5") for c in calls)
        )

    def test_queue_reaches_the_log_even_when_api_fails(self):
        def api(_method, _path, _payload=None):
            raise OSError("connection refused")

        findings = make_findings()
        path = self.write_findings(findings)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            with self.assertRaises(OSError):
                translation_issue.sync_issue(path, api=api, repository="owner/repo")
        lines = out.getvalue().splitlines()
        prefix = translation_issue.QUEUE_LOG_PREFIX + " "
        logged = [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
        # Full text, not the issue's truncated copy.
        self.assertEqual(findings, logged)
        self.assertIn(f"{translation_issue.QUEUE_LOG_PREFIX}-BEGIN count=2", lines)
        self.assertTrue(any(line.startswith("::error ") for line in lines))

    def test_empty_queue_is_logged(self):
        out = io.StringIO()
        translation_issue.log_queue([], out=out)
        self.assertEqual(
            f"{translation_issue.QUEUE_LOG_PREFIX}-EMPTY\n", out.getvalue(),
        )

    def test_missing_findings_file_raises_rather_than_closing(self):
        with self.assertRaises(FileNotFoundError):
            translation_issue.sync_issue(
                "does-not-exist.json",
                api=lambda m, p, payload=None: (200, {}),
                repository="owner/repo",
            )


class MainTests(TempFileTestCase):
    def test_usage_error_on_bad_arguments(self):
        self.assertEqual(2, translation_issue.main([]))
        self.assertEqual(2, translation_issue.main(["frobnicate", "x.json"]))
        self.assertEqual(2, translation_issue.main(["open"]))

    def test_close_takes_no_path_argument(self):
        # The scheduled refresh invokes `close` with no file argument.
        with mock.patch.object(
            translation_issue, "close_issue", return_value=None
        ) as close:
            self.assertEqual(0, translation_issue.main(["close"]))
        close.assert_called_once_with()

    def test_sync_routes_to_open_and_close(self):
        with_findings = self.write_findings(make_findings())
        without = self.write_findings([])
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
