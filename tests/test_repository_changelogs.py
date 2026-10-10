"""Rate-bounded generic repository changelog discovery and reading."""
import base64
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / "helper/globalPlugins/_addonStoreChangelogs.py"
spec = importlib.util.spec_from_file_location("repositoryChangelogsTests", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def response(data, headers=None):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = body
    result.headers = dict(headers or {})
    return result


def tree(*items, truncated=False):
    return response({"tree": list(items), "truncated": truncated})


def blob(path, size=20, **extra):
    return {"path": path, "type": "blob", "mode": "100644", "size": size, **extra}


def content(path, text):
    encoded = base64.encodebytes(text.encode("utf-8")).decode("ascii")
    return response({"type": "file", "path": path, "encoding": "base64", "content": encoded})


class RepositoryChangelogTests(unittest.TestCase):
    def test_tree_discovery_is_one_request_and_filters_to_regular_nonempty_allowlisted_files(self):
        with mock.patch.object(module, "urlopen", return_value=tree(
            blob("CHANGELOG.md"), blob("changes.md"), blob("README.md"),
            blob("changes.txt", size=0), blob("docs/changes.md", mode="120000"),
            {"path": {}, "type": "blob", "mode": "100644", "size": 3},
        )) as opened:
            records, error = module.ReleaseHistory().get("one/alpha", "repositoryIndex")
        self.assertIsNone(error)
        self.assertEqual(["changes.md", "CHANGELOG.md"], [item["path"] for item in records])
        self.assertEqual(1, opened.call_count)
        self.assertEqual("https://api.github.com/repos/one/alpha/git/trees/HEAD?recursive=1",
                         opened.call_args.args[0].full_url)

    def test_selected_repository_reads_one_file_per_action_and_reuses_index_and_page_cache(self):
        firstText = "## 1.0\nInitial release — தமிழ்"
        secondText = "Notes without versions"
        history = module.ReleaseHistory()
        with mock.patch.object(module, "urlopen", side_effect=[
            tree(blob("changes.md"), blob("CHANGELOG.md")),
            content("changes.md", firstText), content("CHANGELOG.md", secondText),
        ]) as opened:
            index, indexError = history.get("one/alpha", "repositoryIndex")
            first, firstError = history.get("one/alpha", "repository")
            firstAgain, cachedError = history.get("one/alpha", "repository")
            second, secondError = history.get("one/alpha", "repository", first.nextPage)
        self.assertIsNone(indexError)
        self.assertEqual(2, len(index))
        self.assertEqual((first, firstError), (firstAgain, cachedError))
        self.assertEqual(firstText, first[0]["text"])
        self.assertEqual(2, first.nextPage)
        self.assertEqual(secondText, second[0]["text"])
        self.assertIsNone(secondError)
        self.assertEqual(3, opened.call_count)
        self.assertTrue(opened.call_args_list[1].args[0].full_url.endswith("/contents/changes.md"))
        rows = module._repositoryRows(types.SimpleNamespace(sourceURL="https://github.com/one/alpha"), first, None)
        self.assertIn("தமிழ்", rows[0][1])
        self.assertEqual(2, rows.nextPage)
        self.assertIn("Read next changelog file", module._historyStatus(rows))
        self.assertNotIn("Older history", module._historyStatus(rows))

    def test_truncated_tree_reports_partial_known_files_without_fabricating_empty_repository(self):
        with mock.patch.object(module, "urlopen", return_value=tree(blob("changes.md"), truncated=True)):
            records, error = module.ReleaseHistory().get("author/other", "repositoryIndex")
        self.assertEqual([{"path": "changes.md", "size": 20}], list(records))
        self.assertEqual("historyLimit", error)
        self.assertTrue(records.limitReached)
        model = types.SimpleNamespace(sourceURL="https://github.com/author/other")
        self.assertTrue(module._sourceHasItems("repository", model, records))
        self.assertEqual((1, 1), module._sourceItemCounts("repository", model, records))

    def test_tree_response_with_zero_remaining_stops_before_content_request(self):
        now = [1000.0]
        history = module.ReleaseHistory(now=lambda: now[0])
        exhausted = tree(blob("changes.md"))
        exhausted.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}
        with mock.patch.object(module, "urlopen", return_value=exhausted) as opened:
            records, error = history.get("author/other", "repository")
        self.assertEqual([], records)
        self.assertEqual("rateLimit", error)
        self.assertEqual(1, opened.call_count)

    def test_content_reader_keeps_strict_bounds_base64_utf8_and_exact_path(self):
        history = module.ReleaseHistory()
        index = module.ReleaseRecords([{"path": "changes.md", "size": 20}])
        index.recordKind = "repositoryIndex"
        key = history._cacheKey("author/other", "repositoryIndex", 1)
        history._cache[key] = (history._now(), index, None)
        history._cacheBytes[key] = module._recordsBytes(index)
        bad = {"type": "file", "path": "changes.md", "encoding": "base64", "content": "%%%"}
        with mock.patch.object(module, "urlopen", return_value=response(bad)):
            self.assertEqual(([], "networkError"), history.get("author/other", "repository"))
        contentKey = history._cacheKey("author/other", "repository", 1)
        history._cache.pop(contentKey, None)
        history._cacheBytes.pop(contentKey, None)
        with mock.patch.object(module, "MAX_RESPONSE_BYTES", 2), mock.patch.object(
            module, "urlopen", return_value=content("changes.md", "large"),
        ):
            self.assertEqual(([], "historyLimit"), history.get("author/other", "repository"))

    def test_non_github_source_never_starts_a_request(self):
        with mock.patch.object(module, "urlopen") as opened:
            rows = module.historyForModel(types.SimpleNamespace(sourceURL="https://example.com/unrelated"),
                                          module.ReleaseHistory(), "repository")
        opened.assert_not_called()
        self.assertEqual("notGitHub", rows.error)


if __name__ == "__main__":
    unittest.main()
