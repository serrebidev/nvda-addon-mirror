"""Availability-filtered changelog source discovery and chooser checks."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "helper/globalPlugins/_addonStoreChangelogs.py"
SPEC = importlib.util.spec_from_file_location("changelogSourceAvailability", PATH)
changelogs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changelogs)


class Model(types.SimpleNamespace):
    pass


class DeferredHistory:
    def __init__(self):
        self.requests = []

    def getAsync(self, repository, callback, source="github", startPage=1, bypassCache=False):
        self.requests.append((repository, source, callback, startPage, bypassCache))


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.model = Model(
            displayName="Example", addonVersionName="2.0",
            sourceURL="https://github.com/owner/project",
        )
        self.parent = Model(_serrebiChangelogAlive=True)
        self.ui = types.ModuleType("ui")
        self.ui.message = mock.Mock()
        self.wx = types.ModuleType("wx")
        self.wx.CallAfter = mock.Mock()

    def test_source_gating_and_meaningful_counts(self):
        setattr(self.model, changelogs.MODEL_CHANGELOG_ATTRIBUTE, "2.0:\nNew\n\n1.0:\nOld")
        releases = [{"tag_name": "2.0", "body": ""}, {"tag_name": "1.0", "body": "Notes"}]
        documents = [
            {"path": "changes.md", "text": "Introduction\n\n## 2.0\nNew\n## 1.0\nOld"},
            {"path": "CHANGELOG.txt", "text": "Unstructured notes"},
            {"path": "empty.md", "text": "   "},
        ]
        commits = [{"sha": "a" * 40, "message": "Change"}, {"sha": "", "message": "invalid"}]
        self.assertTrue(changelogs._sourceHasItems("catalog", self.model, []))
        self.assertTrue(changelogs._sourceHasItems("github", self.model, releases))
        self.assertTrue(changelogs._sourceHasItems("repository", self.model, documents))
        self.assertTrue(changelogs._sourceHasItems("commits", self.model, commits))
        self.assertEqual((2, 0), changelogs._sourceItemCounts("catalog", self.model, []))
        self.assertEqual((2, 0), changelogs._sourceItemCounts("github", self.model, releases))
        self.assertEqual((3, 2), changelogs._sourceItemCounts("repository", self.model, documents))
        self.assertEqual((1, 0), changelogs._sourceItemCounts("commits", self.model, commits))
        for source, records in (("github", []), ("repository", [{"text": " "}]),
                                ("commits", [{"sha": "", "message": ""}])):
            self.assertFalse(changelogs._sourceHasItems(source, self.model, records))

    def test_all_picker_offers_capabilities_without_any_github_request(self):
        feature = changelogs.ChangelogFeature(None)
        feature.history = DeferredHistory()
        setattr(self.model, changelogs.MODEL_CHANGELOG_ATTRIBUTE, "Catalog notes")
        with mock.patch.dict(sys.modules, {"ui": self.ui}), mock.patch.object(
            changelogs, "isSecureDesktop", return_value=False,
        ), mock.patch.object(feature, "_chooseSource", return_value=None) as choose, mock.patch.object(
            changelogs, "urlopen",
        ) as opened:
            feature._discoverSources(self.model, "Example", lambda: self.parent)
        self.assertEqual([], feature.history.requests)
        opened.assert_not_called()
        self.assertEqual((
            ("catalog", None, 0, False), ("github", None, 0, False),
            ("repository", None, 0, False), ("commits", None, 0, False),
        ), choose.call_args.args[1])

    def test_all_selected_github_capability_starts_only_that_source(self):
        for selected in ("github", "repository", "commits"):
            feature = changelogs.ChangelogFeature(None)
            with mock.patch.object(changelogs, "isSecureDesktop", return_value=False), mock.patch.object(
                feature, "_chooseSource", return_value=selected,
            ), mock.patch.object(feature, "_requestHistory") as request:
                feature._discoverSources(self.model, "Example", lambda: self.parent)
            request.assert_called_once_with(
                self.model, "Example", mock.ANY, selected, discovered=None,
            )

    def test_no_local_or_valid_github_source_reports_unavailable_without_request(self):
        feature = changelogs.ChangelogFeature(None)
        self.model.sourceURL = "https://example.com/unrelated"
        with mock.patch.dict(sys.modules, {"ui": self.ui}), mock.patch.object(
            changelogs, "isSecureDesktop", return_value=False,
        ), mock.patch.object(feature, "_chooseSource") as choose, mock.patch.object(
            changelogs, "urlopen",
        ) as opened:
            feature._discoverSources(self.model, "Example", lambda: self.parent)
        choose.assert_not_called()
        opened.assert_not_called()
        message = self.ui.message.call_args.args[0]
        self.assertIn("No changelog sources", message)

    def test_closed_superseded_and_cancelled_picker_cannot_request_viewer(self):
        feature = changelogs.ChangelogFeature(None)
        with mock.patch.object(changelogs, "isSecureDesktop", return_value=False), mock.patch.object(
            feature, "_chooseSource", return_value=None,
        ) as choose, mock.patch.object(feature, "_requestHistory") as request:
            feature._discoverSources(self.model, "Example", lambda: Model(_serrebiChangelogAlive=False))
            feature._discoverSources(self.model, "Example", lambda: self.parent)
        choose.assert_called_once()
        request.assert_not_called()
        def supersede(*_args):
            feature._generation += 1
            return "github"
        with mock.patch.object(changelogs, "isSecureDesktop", return_value=False), mock.patch.object(
            feature, "_chooseSource", side_effect=supersede,
        ), mock.patch.object(feature, "_requestHistory") as request:
            feature._discoverSources(self.model, "Example", lambda: self.parent)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
