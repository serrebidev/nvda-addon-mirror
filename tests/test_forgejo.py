"""The Forgejo author source: config, release state, cache sharing, pins."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock
from urllib.error import HTTPError

import mirror


def http_error(url, code, message):
    error = HTTPError(url, code, message, None, None)
    error.close()
    return error


def bundle_bytes(name="forgejoAddon", version="1.0"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.ini", (
            f"name = {name}\nsummary = Summary\nauthor = Author\n"
            f"version = {version}\nminimumNVDAVersion = 2025.1\n"
            "lastTestedNVDAVersion = 2027.1\n"
        ))
    return stream.getvalue()


def forgejo_releases():
    """Release records shaped like the live Forgejo API: assets carry a
    uuid and no updated_at, and download from the instance host."""
    return [
        {
            "tag_name": "v1.2",
            "name": "1.2",
            "body": "Changes.",
            "draft": False,
            "prerelease": False,
            "created_at": "2026-11-08T21:04:47-07:00",
            "published_at": "2026-11-08T21:04:47-07:00",
            "assets": [
                {
                    "id": 9524,
                    "name": "myAddon-1.2.nvda-addon",
                    "size": 100,
                    "download_count": 1,
                    "created_at": "2026-11-08T21:07:22-07:00",
                    "uuid": "869cd9d9-df92-4bb7-a691-542f84a2e7c9",
                    "browser_download_url": (
                        "https://git.serrebiradio.com/serrebi/demo/"
                        "releases/download/v1.2/myAddon-1.2.nvda-addon"
                    ),
                },
                {
                    "id": 9525,
                    "name": "myAddon-1.2.nvda-addon.sha256",
                    "size": 101,
                    "browser_download_url": (
                        "https://git.serrebiradio.com/serrebi/demo/"
                        "releases/download/v1.2/myAddon-1.2.nvda-addon.sha256"
                    ),
                },
            ],
        },
        {
            "tag_name": "v1.3-beta",
            "name": "1.3 beta",
            "body": "",
            "draft": False,
            "prerelease": True,
            "published_at": "2026-11-09T01:00:00-07:00",
            "assets": [
                {
                    "id": 9600,
                    "name": "myAddon-1.3-beta.nvda-addon",
                    "size": 110,
                    "browser_download_url": (
                        "https://git.serrebiradio.com/serrebi/demo/"
                        "releases/download/v1.3-beta/myAddon-1.3-beta.nvda-addon"
                    ),
                },
            ],
        },
        {
            "tag_name": "v9.9",
            "name": "draft",
            "body": "",
            "draft": True,
            "prerelease": False,
            "assets": [
                {
                    "id": 9700,
                    "name": "myAddon-9.9.nvda-addon",
                    "size": 120,
                    "browser_download_url": (
                        "https://git.serrebiradio.com/serrebi/demo/"
                        "releases/download/v9.9/myAddon-9.9.nvda-addon"
                    ),
                },
            ],
        },
    ]


class ForgejoOwnersConfigTests(unittest.TestCase):
    def test_explicit_repositories_minus_excludes(self):
        spec = {
            "login": "serrebi",
            "repositories": ["demo", "other", "skipped"],
            "exclude_repositories": ["skipped"],
        }
        self.assertEqual(
            ["serrebi/demo", "serrebi/other"],
            mirror._forgejo_owner_repositories(spec),
        )

    def test_empty_login_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no login"):
            mirror._forgejo_owner_repositories({"repositories": ["demo"]})

    def test_loader_reads_owners_and_tolerates_a_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "forgejoOwners.json")
            self.assertEqual([], mirror._load_forgejo_owners(missing))
            with open(missing, "w", encoding="utf-8") as config_file:
                json.dump(
                    {"owners": [{"login": "serrebi", "repositories": ["demo"]}],
                     "logins": ["someone"]},
                    config_file,
                )
            [spec] = mirror._load_forgejo_owners(missing)
            self.assertEqual("serrebi", spec["login"])
            # Bare logins earn no discovery on the Forgejo source: every
            # repository must be listed explicitly.
            self.assertNotIn("someone", [
                owner.get("login")
                for owner in mirror._load_forgejo_owners(missing)
            ])

    def test_committed_config_lists_the_owner_repositories(self):
        owners = mirror._load_forgejo_owners()
        self.assertEqual(["serrebi"], [spec["login"] for spec in owners])
        self.assertEqual(
            {"NVDARemoteAudioClient", "Reloq-C", "AppleMusicNVDA",
             "logCollector", "signal-desktop-enhancements",
             "nvda-addon-mirror"},
            {repo.split("/", 1)[1] for repo in
             mirror._forgejo_owner_repositories(owners[0])},
        )


class ForgejoReleaseStateTests(unittest.TestCase):
    def test_github_shaped_records_become_candidates(self):
        seen = []

        def conditional(url, etag=None, timeout=120):
            seen.append((url, etag))
            return forgejo_releases(), None, False

        with mock.patch.object(
            mirror, "_forgejo_json_conditional", side_effect=conditional
        ):
            candidates, state = mirror._forgejo_release_asset_state(
                "serrebi/demo"
            )
        [(url, etag)] = seen
        self.assertEqual(
            f"{mirror.FORGEJO_API}/repos/serrebi/demo/releases?limit=20",
            url,
        )
        self.assertIsNone(etag)
        by_channel = {item["channel"]: item for item in candidates}
        # The draft release and the .sha256 sidecar are not candidates.
        self.assertEqual({"stable", "beta"}, set(by_channel))
        stable = by_channel["stable"]
        self.assertEqual("serrebi/demo", stable["repo"])
        self.assertEqual("myAddon-1.2.nvda-addon", stable["asset_name"])
        self.assertEqual("v1.2", stable["release_tag"])
        self.assertEqual("Changes.", stable["changelog"])
        self.assertTrue(stable["download_url"].startswith(mirror.FORGEJO_WEB))
        self.assertIn("myAddon-1.2.nvda-addon", stable["cache_key"])
        self.assertEqual([1, 3, 0], state["latest_version"])

    def test_not_modified_reuses_cached_candidates(self):
        cached = [{"repo": "serrebi/demo"}]
        previous = {"etag": '"x"', "candidates": cached,
                    "latest_version": [1, 2, 0]}
        with mock.patch.object(
            mirror, "_forgejo_json_conditional",
            return_value=(None, '"x"', True),
        ) as conditional:
            got, state = mirror._forgejo_release_asset_state(
                "serrebi/demo", previous
            )
        conditional.assert_called_once()
        self.assertEqual(cached, got)
        self.assertIs(previous, state)

    def test_not_modified_without_cache_is_an_error(self):
        previous = {"etag": '"x"', "latest_version": [1, 2, 0]}
        with mock.patch.object(
            mirror, "_forgejo_json_conditional",
            return_value=(None, '"x"', True),
        ):
            with self.assertRaisesRegex(RuntimeError, "304 without cached"):
                mirror._forgejo_release_asset_state("serrebi/demo", previous)


class ForgejoFetchTests(unittest.TestCase):
    def write_config(self, directory):
        config_path = os.path.join(directory, "forgejoOwners.json")
        with open(config_path, "w", encoding="utf-8") as config_file:
            json.dump(
                {"owners": [{"login": "serrebi", "repositories": ["demo"]}]},
                config_file,
            )
        return config_path

    def test_validated_entry_points_at_forgejo(self):
        raw = bundle_bytes(version="1.2")
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            cache_path = os.path.join(directory, "cache.json")
            with mock.patch.object(
                mirror, "_forgejo_json_conditional",
                return_value=(forgejo_releases(), None, False),
            ), mock.patch.object(mirror, "http_get", return_value=raw):
                entries = mirror.fetch_forgejo_owners(config_path, cache_path)
        self.assertEqual(2, len(entries))
        stable = next(
            entry for entry in entries if entry["channel"] == "stable"
        )
        self.assertEqual("forgejo_owner", stable["source"])
        self.assertEqual("forgejoAddon", stable["name"])
        self.assertEqual("1.2", stable["version"])
        self.assertEqual(
            f"{mirror.FORGEJO_WEB}/serrebi/demo", stable["source_url"]
        )
        self.assertTrue(
            stable["download_url"].startswith(mirror.FORGEJO_WEB)
        )

    def test_second_run_reuses_the_shared_cache_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            cache_path = os.path.join(directory, "cache.json")
            with mock.patch.object(
                mirror, "_forgejo_json_conditional",
                return_value=(forgejo_releases(), None, False),
            ), mock.patch.object(
                mirror, "http_get", return_value=bundle_bytes()
            ):
                first = mirror.fetch_forgejo_owners(config_path, cache_path)
            with open(cache_path, encoding="utf-8") as cache_file:
                written = json.load(cache_file)
            self.assertIn(mirror.FORGEJO_CACHE_SECTION, written)
            section = written[mirror.FORGEJO_CACHE_SECTION]
            self.assertIn("serrebi/demo", section["release_state"])
            self.assertTrue(section["entries"])
            with mock.patch.object(
                mirror, "_forgejo_json_conditional",
                return_value=(forgejo_releases(), None, False),
            ), mock.patch.object(
                mirror, "http_get",
                side_effect=AssertionError("cached bundle refetched"),
            ):
                second = mirror.fetch_forgejo_owners(config_path, cache_path)
        self.assertEqual(
            {(entry["name"], entry["channel"]) for entry in first},
            {(entry["name"], entry["channel"]) for entry in second},
        )

    def test_github_cache_keys_survive_a_forgejo_write(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            cache_path = os.path.join(directory, "cache.json")
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                json.dump({"__discovery__": {"marker": True}}, cache_file)
            with mock.patch.object(
                mirror, "_forgejo_json_conditional",
                return_value=([], None, False),
            ):
                self.assertEqual(
                    [], mirror.fetch_forgejo_owners(config_path, cache_path)
                )
            with open(cache_path, encoding="utf-8") as cache_file:
                written = json.load(cache_file)
        self.assertEqual({"marker": True}, written["__discovery__"])
        self.assertIn(mirror.FORGEJO_CACHE_SECTION, written)

    def test_failed_repository_stops_publication(self):
        def conditional(url, etag=None, timeout=120):
            raise http_error(url, 500, "Internal Server Error")

        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            cache_path = os.path.join(directory, "cache.json")
            with mock.patch.object(
                mirror, "_forgejo_json_conditional", side_effect=conditional
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "refusing to publish incomplete Forgejo author discovery",
                ):
                    mirror.fetch_forgejo_owners(config_path, cache_path)

    def test_gone_asset_is_rejected_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            cache_path = os.path.join(directory, "cache.json")

            def download(url, timeout=120, headers=None):
                raise http_error(url, 404, "Not Found")

            with mock.patch.object(
                mirror, "_forgejo_json_conditional",
                return_value=(forgejo_releases(), None, False),
            ), mock.patch.object(mirror, "http_get", side_effect=download):
                entries = mirror.fetch_forgejo_owners(config_path, cache_path)
            with open(cache_path, encoding="utf-8") as cache_file:
                written = json.load(cache_file)
        self.assertEqual([], entries)
        self.assertEqual(2, len(mirror.FORGEJO_OWNER_REJECTIONS))
        rejection = mirror.FORGEJO_OWNER_REJECTIONS[0]
        self.assertEqual("forgejo_owner", rejection["source"])
        self.assertIn("HTTP 404", rejection["reason"])
        invalid = next(iter(
            written[mirror.FORGEJO_CACHE_SECTION]["entries"].values()
        ))
        self.assertTrue(invalid["_repo"].startswith("forgejo:"))


class SharedCacheTests(unittest.TestCase):
    def test_github_write_carries_the_forgejo_section(self):
        section = {"release_state": {"serrebi/demo": {"etag": None}},
                   "entries": {}}
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "githubOwners.json")
            cache_path = os.path.join(directory, "githubOwnerCache.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"owners": []}, config_file)
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                json.dump({mirror.FORGEJO_CACHE_SECTION: section}, cache_file)
            with mock.patch.object(mirror, "GITHUB_TOKEN", ""), \
                    mock.patch.object(
                        mirror, "_github_owner_repositories",
                        return_value=([], {}),
                    ):
                mirror.fetch_github_owners(config_path, cache_path)
            with open(cache_path, encoding="utf-8") as cache_file:
                written = json.load(cache_file)
        self.assertEqual(section, written[mirror.FORGEJO_CACHE_SECTION])
        self.assertIn("__discovery__", written)

    def test_github_repo_scan_ignores_forgejo_markers(self):
        cache = {
            "a": {"source_url": "https://github.com/owner/repo"},
            "b": {"_repo": "forgejo:serrebi/demo"},
            "c": {"_repo": "owner/plain"},
            mirror.FORGEJO_CACHE_SECTION: {"release_state": {}, "entries": {}},
        }
        self.assertEqual(
            {"owner/repo", "owner/plain"},
            mirror._cached_github_repositories(cache),
        )


class ForgejoPriorityTests(unittest.TestCase):
    def entry(self, source, version="1.1"):
        return {
            "name": "sharedAddon",
            "channel": "stable",
            "version": version,
            "source": source,
            "summary": "Summary",
            "description": "Description.",
            "download_url": "https://example.invalid/shared.nvda-addon",
        }

    def test_forgejo_release_wins_over_its_github_sibling(self):
        [winner] = mirror.dedupe([
            self.entry("github_owner"), self.entry("forgejo_owner"),
        ])
        self.assertEqual("forgejo_owner", winner["source"])

    def test_forgejo_release_wins_over_a_newer_github_sibling(self):
        [winner] = mirror.dedupe([
            self.entry("github_owner", "9.9"),
            self.entry("forgejo_owner", "1.1"),
        ])
        self.assertEqual("forgejo_owner", winner["source"])

    def test_explicit_pin_still_wins_over_forgejo(self):
        [winner] = mirror.dedupe([
            self.entry("forgejo_owner", "9.9"), self.entry("pinned", "1.1"),
        ])
        self.assertEqual("pinned", winner["source"])

    def test_priority_order_is_forgejo_above_github(self):
        priority = mirror.SOURCE_PRIORITY
        self.assertGreater(
            priority["forgejo_owner"], priority["github_owner"]
        )
        self.assertGreater(priority["pinned"], priority["forgejo_owner"])


class ForgejoPinnedTests(unittest.TestCase):
    def test_forgejo_pin_uses_the_instance_urls_and_label(self):
        bundle = bundle_bytes("forgedAddon", "1.2")
        releases = [{
            "prerelease": False,
            "tag_name": "v1.2",
            "name": "1.2",
            "body": "Changes.",
            "published_at": "2026-11-08T21:04:47-07:00",
            "assets": [{"name": "forgedAddon-1.2.nvda-addon",
                        "browser_download_url": "https://example.org/x"}],
        }]
        seen = []

        def get(url, timeout=120, headers=None):
            seen.append(url)
            return json.dumps(releases).encode("utf-8")

        spec = {"host": "forgejo", "fork_policy": "include"}
        with mock.patch.object(mirror, "http_get", side_effect=get), \
                mock.patch.object(
                    mirror, "cached_pinned_bundle", return_value=bundle
                ):
            [entry] = mirror._fetch_one_pinned(
                spec, "serrebi/demo", "forgedAddon"
            )
        self.assertEqual(
            [f"{mirror.FORGEJO_API}/repos/serrebi/demo/releases?limit=20"],
            seen,
        )
        self.assertEqual(
            f"{mirror.FORGEJO_WEB}/serrebi/demo", entry["homepage"]
        )
        self.assertEqual(
            f"{mirror.FORGEJO_WEB}/serrebi/demo", entry["source_url"]
        )
        self.assertEqual(
            f"{mirror.FORGEJO_WEB}/serrebi/demo/releases/download/"
            "v1.2/forgedAddon-1.2.nvda-addon",
            entry["download_url"],
        )
        self.assertEqual(
            mirror.PINNED_FORGEJO_SOURCE_LABEL,
            entry["store_source_label"],
        )

    def test_github_pin_carries_no_forgejo_label(self):
        releases = [{
            "prerelease": False,
            "tag_name": "v1.2",
            "name": "1.2",
            "assets": [{"name": "x-1.2.nvda-addon",
                        "browser_download_url": "https://example.org/x"}],
        }]
        with mock.patch.object(
            mirror, "http_get",
            return_value=json.dumps(releases).encode("utf-8"),
        ), mock.patch.object(
            mirror, "cached_pinned_bundle",
            return_value=bundle_bytes("pinnedAddon", "1.2"),
        ):
            [entry] = mirror._fetch_one_pinned(
                {"fork_policy": "include"}, "owner/x", "pinnedAddon",
            )
        self.assertTrue(entry["homepage"].startswith("https://github.com/"))
        self.assertNotIn("store_source_label", entry)

    def test_unknown_pin_host_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "unknown host"):
            mirror._fetch_one_pinned(
                {"host": "bitbucket"}, "owner/x", "someAddon",
            )

    def test_default_pin_policy_checks_the_forgejo_parent(self):
        with mock.patch.object(
            mirror, "_forgejo_fork_release_qualifies", return_value=True
        ) as qualifies:
            self.assertTrue(mirror._pinned_fork_release_qualifies(
                {}, "serrebi/demo", (1, 0, 0), host="forgejo",
            ))
        qualifies.assert_called_once_with("serrebi/demo", (1, 0, 0))

    def test_forgejo_original_needs_no_release_comparison(self):
        with mock.patch.object(
            mirror, "_forgejo_json", return_value={"fork": False}
        ), mock.patch.object(
            mirror,
            "_forgejo_release_asset_state",
            side_effect=AssertionError("no parent to compare"),
        ):
            self.assertTrue(
                mirror._forgejo_fork_release_qualifies(
                    "serrebi/demo", (1, 0, 0)
                )
            )

    def test_forgejo_fork_must_outrelease_its_parent(self):
        metadata = {"fork": True, "parent": {"full_name": "up/stream"}}

        def state(repo, previous_state=None):
            versions = {"serrebi/demo": [1, 2, 0], "up/stream": [1, 1, 0]}
            return [], {"latest_version": versions[repo]}

        with mock.patch.object(
            mirror, "_forgejo_json", return_value=metadata
        ), mock.patch.object(
            mirror, "_forgejo_release_asset_state", side_effect=state
        ):
            self.assertTrue(
                mirror._forgejo_fork_release_qualifies(
                    "serrebi/demo", (1, 2, 0)
                )
            )
            self.assertFalse(
                mirror._forgejo_fork_release_qualifies(
                    "serrebi/demo", (1, 0, 0)
                )
            )


class ForgejoAssetEntryTests(unittest.TestCase):
    def candidate(self):
        return {
            "repo": "serrebi/demo",
            "channel": "stable",
            "asset_name": "myAddon-1.2.nvda-addon",
            "download_url": "https://git.serrebiradio.com/serrebi/demo/x",
            "release_tag": "v1.2",
            "published_at": "2026-11-08T21:04:47-07:00",
            "changelog": "Changes.",
        }

    def test_builder_honours_source_and_repo_base(self):
        with mock.patch.object(
            mirror, "http_get", return_value=bundle_bytes()
        ):
            entry = mirror._forgejo_asset_entry(self.candidate())
        self.assertEqual("forgejo_owner", entry["source"])
        self.assertEqual(
            f"{mirror.FORGEJO_WEB}/serrebi/demo", entry["source_url"]
        )
        self.assertEqual(
            f"{mirror.FORGEJO_WEB}/serrebi/demo", entry["homepage"]
        )

    def test_github_builder_defaults_are_unchanged(self):
        candidate = dict(
            self.candidate(), repo="owner/demo",
            download_url="https://example.invalid/x",
        )
        with mock.patch.object(
            mirror, "http_get", return_value=bundle_bytes()
        ):
            entry = mirror._github_asset_entry(candidate)
        self.assertEqual("github_owner", entry["source"])
        self.assertEqual("https://github.com/owner/demo", entry["source_url"])


if __name__ == "__main__":
    unittest.main()
