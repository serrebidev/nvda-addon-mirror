import importlib.util
import os
import pathlib
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock


_PATH = pathlib.Path(__file__).parents[1] / "helper" / "globalPlugins" / "_addonStoreDiscovery.py"
_SPEC = importlib.util.spec_from_file_location("discovery", _PATH)
discovery = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(discovery)


def model(addonId, name, description="", author=None, publisher=None, sourceURL=""):
    return SimpleNamespace(
        addonId=addonId, displayName=name, description=description, author=author,
        publisher=publisher, sourceURL=sourceURL,
    )


class DiscoveryTests(unittest.TestCase):
    def test_author_matches_deduplicate_channels_and_separate_evidence(self):
        selected = model("one", "One", author="Ada", sourceURL="https://github.com/Ada/tools")
        matches = discovery.authorMatches(selected, [
            selected,
            model("two", "Two", publisher="ada"),
            model("two", "Two dev", publisher="Ada"),
            model("three", "Three", sourceURL="https://github.com/ada/other"),
        ])
        self.assertEqual(["one", "two", "three"], [item[0].addonId for item in matches])
        self.assertEqual(((discovery.AUTHOR_PUBLISHER, ()),), matches[1][1])
        self.assertEqual(((discovery.REPOSITORY_OWNER, ()),), matches[2][1])

    def test_similarity_is_weighted_excludes_self_and_explains_match(self):
        selected = model("one", "Network Tools", "Manage network profiles")
        matches = discovery.similarMatches(selected, [
            selected,
            model("two", "Network Manager", "Profiles and settings"),
            model("three", "Profiles", "Manage network profiles"),
        ])
        self.assertEqual(["two", "three"], [item[0].addonId for item in matches])
        self.assertIn((discovery.SIMILAR_TITLE, ("network",)), matches[0][1])
        self.assertIn(
            (discovery.SIMILAR_DESCRIPTION, ("manage", "network", "profiles")),
            matches[1][1],
        )

    def test_repository_accepts_only_plain_https_github_repo(self):
        self.assertEqual(("owner", "repo"), discovery.githubRepository("https://github.com/Owner/repo.git"))
        self.assertIsNone(discovery.githubRepository("http://github.com/owner/repo"))
        self.assertIsNone(discovery.githubRepository("https://github.com/owner/repo/issues"))
        self.assertIsNone(discovery.githubRepository("https://user@github.com/owner/repo"))
        for suffix in ("?download=1", "#fragment"):
            self.assertIsNone(discovery.githubRepository("https://github.com/owner/repo" + suffix))
        self.assertIsNone(discovery.githubRepository("https://github.com:444/owner/repo"))
        self.assertIsNone(discovery.githubRepository("https://github.com:invalid/owner/repo"))
        self.assertIsNone(discovery.githubRepository("https://[broken"))

    def test_cross_field_similarity_has_an_explanation(self):
        matches = discovery.similarMatches(model("one", "Network"), [model("two", "Tools", "Network")])
        self.assertEqual(
            ((discovery.SIMILAR_TERMS, ("network",)),),
            matches[0][1],
        )

    def test_similarity_keeps_later_matching_channel_when_first_does_not_match(self):
        matches = discovery.similarMatches(model("one", "Network"), [
            model("two", "Unrelated"),
            model("two", "Network helper"),
        ])
        self.assertEqual(["two"], [item[0].addonId for item in matches])
        self.assertEqual("Network helper", matches[0][0].displayName)

    def test_missing_author_identity_has_no_invented_matches(self):
        selected = model("one", "One")
        self.assertEqual([], discovery.authorMatches(selected, [selected, model("two", "Two")]))

    def test_author_name_can_use_repository_owner_without_falsifying_catalog_metadata(self):
        addon = model("one", "One", sourceURL="https://github.com/Owner/repository")
        self.assertIsNone(discovery.catalogAuthor(addon))
        self.assertEqual("Owner", discovery.authorName(addon))
        self.assertEqual(("Owner", "repository"), discovery.repositoryDisplay(addon))

    def test_clone_uses_argument_vector_and_refuses_existing_destination(self):
        with mock.patch.object(discovery.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            result = discovery.cloneRepository("https://github.com/owner/repo", "G:\\new-repo")
        self.assertEqual("G:\\new-repo", result)
        self.assertEqual(
            ["git", "clone", "--", "https://github.com/owner/repo", "G:\\new-repo"],
            run.call_args.args[0],
        )
        with mock.patch.object(discovery.os.path, "exists", return_value=True):
            with self.assertRaises(ValueError):
                discovery.cloneRepository("https://github.com/owner/repo", "G:\\new-repo")

    def test_clone_returns_typed_failures_without_spoken_english(self):
        with mock.patch.object(discovery.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(discovery.CloneFailure) as raised:
                discovery.cloneRepository("https://github.com/owner/repo", "G:\\new-repo")
        self.assertEqual("cloneFailed", raised.exception.code)
        with mock.patch.object(discovery.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            with self.assertRaises(discovery.CloneFailure) as raised:
                discovery.cloneRepository("https://github.com/owner/repo", "G:\\new-repo")
        self.assertEqual("timeout", raised.exception.code)
        with mock.patch.object(discovery.subprocess, "run", side_effect=OSError("missing git")):
            with self.assertRaises(discovery.CloneFailure) as raised:
                discovery.cloneRepository("https://github.com/owner/repo", "G:\\new-repo")
        self.assertEqual("gitUnavailable", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
