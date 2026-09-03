import json
import io
import os
import tempfile
import unittest
import zipfile
from unittest import mock
from urllib.error import HTTPError

import mirror


def httpError(url, code, message):
    """Build an HTTPError without leaking its response body.

    urllib gives an HTTPError a file-like body even when none is supplied, and
    that body warns (ResourceWarning) when it is garbage collected unclosed.
    The mirror only ever reads .code, so close it up front.
    """
    error = HTTPError(url, code, message, None, None)
    error.close()
    return error


class PinnedVersionTests(unittest.TestCase):
    def test_newer_asset_version_wins_over_stale_manifest(self):
        self.assertEqual(
            "2.0.0",
            mirror._select_pinned_version(
                "1.5.0",
                "pantheraspeech-2.0.0.nvda-addon",
                "pantheraspeech/v2.0.0",
            ),
        )

    def test_manifest_spelling_is_kept_when_versions_match(self):
        self.assertEqual(
            "19.1.3-RS",
            mirror._select_pinned_version(
                "19.1.3-RS",
                "Eloquence-19.1.3-RS.nvda-addon",
                "v19.1.3-RS",
            ),
        )


class PinnedCompletenessTests(unittest.TestCase):
    def test_failed_pin_stops_incomplete_publication(self):
        config = {
            "pinned": [
                {"repo": "example/good", "addon_id": "good"},
                {"repo": "example/bad", "addon_id": "bad"},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pinned.json")
            with open(path, "w", encoding="utf-8") as config_file:
                json.dump(config, config_file)

            def fetch(spec, repo, addon_id):
                if repo == "example/bad":
                    raise RuntimeError("release unavailable")
                return [{"name": addon_id}]

            with mock.patch.object(mirror, "_fetch_one_pinned", side_effect=fetch):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "refusing to publish an incomplete pinned add-on set",
                ):
                    mirror.fetch_pinned(path)


class PinnedConfigurationTests(unittest.TestCase):
    def test_serrebi_helper_and_log_collector_are_pinned(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "pinned.json",
        )
        with open(path, "r", encoding="utf-8") as config_file:
            pinned = json.load(config_file)["pinned"]

        configured = {
            (entry.get("repo"), entry.get("addon_id"))
            for entry in pinned
        }
        self.assertIn(
            ("serrebidev/nvda-addon-mirror", "addonStoreMirror"),
            configured,
        )
        self.assertIn(
            ("serrebidev/logCollector", "logCollectorAndFixesFromSerrebi"),
            configured,
        )

    def test_all_eloquence_variants_have_unique_ids_and_names(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "pinned.json",
        )
        with open(path, "r", encoding="utf-8") as config_file:
            pinned = json.load(config_file)["pinned"]

        eloquence = [
            entry for entry in pinned
            if "eloquence" in entry.get("repo", "").casefold()
        ]
        self.assertEqual(4, len(eloquence))
        self.assertEqual(4, len({entry["repo"].casefold() for entry in eloquence}))
        self.assertEqual(4, len({entry["addon_id"].casefold() for entry in eloquence}))
        self.assertEqual(4, len({entry["summary"].casefold() for entry in eloquence}))

        by_repo = {entry["repo"]: entry for entry in eloquence}
        self.assertEqual("include", by_repo["hozosch/eloquence_64"]["fork_policy"])
        self.assertEqual("include", by_repo["Nick6489/Eloquence64RS"]["fork_policy"])

class NvdaReleaseMetadataTests(unittest.TestCase):
    @staticmethod
    def _entry(version, back_compat=(2026, 1, 0), experimental=False):
        entry = {
            "apiVer": dict(zip(("major", "minor", "patch"), version)),
            "backCompatTo": dict(
                zip(("major", "minor", "patch"), back_compat)
            ),
        }
        if experimental:
            entry["experimental"] = True
        return entry

    def test_new_versions_are_added_without_pruning_experimental(self):
        entries = [
            # No NVDA before 2025.1 can be pointed at a mirror: 2023.2 through
            # 2024.4 hardcode the store address.
            self._entry((2023, 3, 0)),
            self._entry((2024, 4, 2)),
            self._entry((2025, 3, 3)),
            self._entry((2026, 1, 1)),
            self._entry((2026, 2, 0)),
            self._entry((2026, 3, 0), experimental=True),
            self._entry((2027, 1, 0)),
        ]
        self.assertEqual(
            [
                "2027.1.0",
                "2026.3.0",
                "2026.2.0",
                "2026.1.1",
                "2025.3.3",
            ],
            mirror.published_nvda_api_versions(entries),
        )

    def test_bundled_api_versions_include_nvda_2026_2(self):
        versions = mirror.load_nvda_api_versions()
        self.assertEqual((2026, 1, 0), versions["2026.2.0"])

    def test_scheduled_refresh_prefers_live_datastore_metadata(self):
        bundled = [self._entry((2026, 2, 0), back_compat=(2025, 1, 0))]
        live = [self._entry((2026, 2, 0)), self._entry((2027, 1, 0))]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nvdaAPIVersions.json")
            with open(path, "w", encoding="utf-8") as metadata_file:
                json.dump(bundled, metadata_file)
            with mock.patch.object(
                mirror,
                "http_get_json",
                return_value=live,
            ) as fetch:
                entries = mirror.load_nvda_api_version_entries(
                    path=path,
                    refresh=True,
                )
        self.assertEqual(
            ["2027.1.0", "2026.2.0"],
            mirror.published_nvda_api_versions(entries),
        )
        self.assertEqual(
            (2026, 1, 0),
            mirror.nvda_api_versions_from_entries(entries)["2026.2.0"],
        )
        fetch.assert_called_once_with(mirror.NVDA_API_VERSIONS_URL, timeout=30)

    def test_scheduled_refresh_falls_back_to_bundled_metadata(self):
        bundled = [self._entry((2026, 2, 0))]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nvdaAPIVersions.json")
            with open(path, "w", encoding="utf-8") as metadata_file:
                json.dump(bundled, metadata_file)
            with mock.patch.object(
                mirror,
                "http_get_json",
                side_effect=OSError("offline"),
            ):
                entries = mirror.load_nvda_api_version_entries(
                    path=path,
                    refresh=True,
                )
        self.assertEqual(bundled, entries)


class PinnedForkPolicyTests(unittest.TestCase):
    def test_explicitly_included_variant_does_not_compare_parent_version(self):
        with mock.patch.object(
            mirror,
            "_github_fork_release_qualifies",
            side_effect=AssertionError("parent comparison should be bypassed"),
        ):
            self.assertTrue(
                mirror._pinned_fork_release_qualifies(
                    {"fork_policy": "include"},
                    "example/variant",
                    (1, 0, 0),
                )
            )

    def test_default_policy_requires_newer_fork(self):
        with mock.patch.object(
            mirror,
            "_github_fork_release_qualifies",
            return_value=False,
        ) as qualifies:
            self.assertFalse(
                mirror._pinned_fork_release_qualifies(
                    {}, "example/fork", (1, 0, 0)
                )
            )
        qualifies.assert_called_once_with("example/fork", (1, 0, 0))

    def test_unknown_policy_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "invalid pinned fork_policy"):
            mirror._pinned_fork_release_qualifies(
                {"fork_policy": "sometimes"},
                "example/fork",
                (1, 0, 0),
            )


class DedupeTests(unittest.TestCase):
    def test_pinned_release_wins_over_stale_bestmidi_metadata(self):
        shared = {
            "name": "pantheraspeech",
            "channel": "stable",
            "download_url": "https://example.invalid/panthera.nvda-addon",
            "summary": "Panthera Speech",
            "description": "",
        }
        bestmidi = dict(shared, source="bestmidi", version="1.5.0")
        pinned = dict(shared, source="pinned", version="2.0.1")

        self.assertEqual("2.0.1", mirror.dedupe([bestmidi, pinned])[0]["version"])

    def test_addon_ids_are_deduplicated_case_insensitively(self):
        shared = {
            "channel": "stable",
            "download_url": "https://example.invalid/addon.nvda-addon",
            "summary": "Example",
            "description": "",
        }
        official = dict(shared, name="ExampleAddon", source="official")
        community = dict(shared, name="exampleaddon", source="bestmidi")

        result = mirror.dedupe([community, official])
        self.assertEqual(1, len(result))
        self.assertEqual("ExampleAddon", result[0]["name"])

    def test_direct_author_release_wins_over_store_copy(self):
        shared = {
            "name": "exampleAddon",
            "channel": "stable",
            "download_url": "https://example.invalid/example.nvda-addon",
            "summary": "Example",
            "description": "",
        }
        official = dict(shared, source="official", version="1.0")
        author = dict(shared, source="github_owner", version="1.1")

        self.assertEqual("1.1", mirror.dedupe([official, author])[0]["version"])


class TransformTests(unittest.TestCase):
    def test_store_source_is_exposed_with_a_human_readable_name(self):
        entry = {
            "name": "exampleAddon",
            "summary": "Example",
            "description": "An example add-on.",
            "author": "Example Author",
            "version": "1.0",
            "channel": "stable",
            "download_url": "https://example.invalid/example.nvda-addon",
            "source": "ru",
        }

        result = mirror.transform(entry, "a" * 64)

        self.assertEqual("NVDA Add-ons RU", result["storeSource"])

    def test_every_configured_source_has_a_display_name(self):
        self.assertEqual(
            {"official", "pinned", "github_owner", "ru", "bestmidi", "es"},
            set(mirror.STORE_SOURCE_LABELS),
        )


class SpanishCatalogTests(unittest.TestCase):
    def test_aliases_are_not_mistaken_for_original_addons(self):
        official = {"name": "ifInterpreters", "source": "official"}
        alias = {
            "name": "IF Interpreters",
            "catalog_name": "IF Interpreters",
            "catalog_file": "ifinterpreters",
            "source": "es",
        }
        original = {
            "name": "newSpanishAddon",
            "catalog_name": "newSpanishAddon",
            "catalog_file": "newspanishaddon",
            "source": "es",
        }

        result = mirror.keep_original_es_entries([official, alias, original])
        self.assertEqual([official, original], result)

    def test_known_product_name_maps_to_internal_id(self):
        community = {"name": "codefactory-py3", "source": "ru"}
        spanish = {
            "name": "codefactory",
            "catalog_name": "codefactory",
            "catalog_file": "codefactory",
            "source": "es",
        }

        self.assertEqual(
            [community],
            mirror.keep_original_es_entries([community, spanish]),
        )

    def test_dev_entry_does_not_hide_stable_spanish_release(self):
        russian_dev = {
            "name": "exampleAddon",
            "channel": "dev",
            "source": "ru",
        }
        spanish_stable = {
            "name": "exampleAddon",
            "catalog_name": "exampleAddon",
            "catalog_file": "exampleAddon",
            "channel": "stable",
            "source": "es",
        }

        self.assertEqual(
            [russian_dev, spanish_stable],
            mirror.keep_original_es_entries([russian_dev, spanish_stable]),
        )


class GitHubOwnerTests(unittest.TestCase):
    def test_release_tag_version_rejects_incidental_digits(self):
        self.assertIsNone(mirror._release_tag_version("sound_lib1"))
        self.assertEqual((0, 2, 1), mirror._release_tag_version("v0.2.1"))
        self.assertEqual((301, 0, 0), mirror._release_tag_version("v-301"))

    def test_owner_repository_discovery_tracks_fork_parents(self):
        response = {
            "owner0": {
                "repositories": {
                    "nodes": [
                        {"nameWithOwner": "example/original", "isFork": False},
                        {
                            "nameWithOwner": "example/forked",
                            "isFork": True,
                            "parent": {"nameWithOwner": "upstream/original"},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with mock.patch.object(mirror, "_github_graphql", return_value=response):
            repositories, fork_parents = mirror._github_owner_repository_names(
                [{"login": "example"}]
            )

        self.assertEqual({"example/original", "example/forked"}, repositories)
        self.assertEqual(
            {"example/forked": "upstream/original"},
            fork_parents,
        )

    def test_owner_repository_exclusion_overrides_newer_fork_policy(self):
        response = {
            "owner0": {
                "repositories": {
                    "nodes": [
                        {
                            "nameWithOwner": "example/tdesktopnvda",
                            "isFork": True,
                            "parent": {"nameWithOwner": "upstream/tdesktopnvda"},
                        },
                        {"nameWithOwner": "example/original", "isFork": False},
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with mock.patch.object(mirror, "_github_graphql", return_value=response):
            repositories, fork_parents = mirror._github_owner_repository_names(
                [{"login": "example", "exclude_repositories": ["tdesktopnvda"]}]
            )

        self.assertEqual({"example/original"}, repositories)
        self.assertEqual({}, fork_parents)

    def test_deleted_owner_account_is_skipped(self):
        response = {
            "owner0": None,
            "owner1": {
                "repositories": {
                    "nodes": [{"nameWithOwner": "present/addon", "isFork": False}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            },
        }
        with mock.patch.object(mirror, "_github_graphql", return_value=response):
            repositories, fork_parents = mirror._github_owner_repository_names(
                [{"login": "deleted"}, {"login": "present"}]
            )

        self.assertEqual({"present/addon"}, repositories)
        self.assertEqual({}, fork_parents)

    def test_every_owner_missing_stops_publication(self):
        with mock.patch.object(
            mirror, "_github_graphql", return_value={"owner0": None}
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "no configured GitHub account resolved",
            ):
                mirror._github_owner_repository_names([{"login": "deleted"}])

    def test_deleted_repository_is_dropped_instead_of_failing_the_build(self):
        cache = {
            "__discovery__": {
                "addon_repositories": ["example/gone", "example/live"],
                "scanned_repositories": ["example/gone", "example/live"],
                "release_state": {"example/gone": {}, "example/live": {}},
            }
        }
        state = ([], {"etag": None, "candidates": [], "latest_version": None})

        def release_state(repo, previous_state=None):
            if repo == "example/gone":
                raise httpError(
                    "https://api.github.invalid", 404, "Not Found"
                )
            return state

        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "githubOwners.json")
            cache_path = os.path.join(directory, "githubOwnerCache.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"logins": ["example"]}, config_file)
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                json.dump(cache, cache_file)

            with mock.patch.object(mirror, "GITHUB_TOKEN", ""),                 mock.patch.object(
                    mirror,
                    "_github_owner_repositories",
                    return_value=(["example/live"], {}),
                ),                 mock.patch.object(
                    mirror, "_github_release_asset_state", side_effect=release_state
                ):
                entries = mirror.fetch_github_owners(config_path, cache_path)

            with open(cache_path, "r", encoding="utf-8") as cache_file:
                written = json.load(cache_file)

        self.assertEqual([], entries)
        discovery = written["__discovery__"]
        self.assertEqual(["example/live"], discovery["addon_repositories"])
        self.assertEqual(["example/live"], discovery["scanned_repositories"])
        self.assertNotIn("example/gone", discovery["release_state"])

    def test_missing_configured_repository_still_stops_publication(self):
        cache = {
            "__discovery__": {
                "addon_repositories": ["example/gone"],
                "scanned_repositories": ["example/gone"],
            }
        }

        def release_state(repo, previous_state=None):
            raise httpError(
                "https://api.github.invalid", 404, "Not Found"
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "githubOwners.json")
            cache_path = os.path.join(directory, "githubOwnerCache.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump(
                    {"owners": [{"login": "example", "repositories": ["gone"]}]},
                    config_file,
                )
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                json.dump(cache, cache_file)

            with mock.patch.object(mirror, "GITHUB_TOKEN", ""),                 mock.patch.object(
                    mirror,
                    "_github_owner_repositories",
                    return_value=(["example/gone"], {}),
                ),                 mock.patch.object(
                    mirror, "_github_release_asset_state", side_effect=release_state
                ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "refusing to publish incomplete GitHub author discovery",
                ):
                    mirror.fetch_github_owners(config_path, cache_path)

    def test_first_time_repository_checks_are_rationed_across_runs(self):
        cache = {
            "__discovery__": {
                "addon_repositories": [],
                "scanned_repositories": ["example/a", "example/b", "example/c"],
                "pending_repositories": ["example/a", "example/b", "example/c"],
            }
        }
        checked = []

        def release_state(repo, previous_state=None):
            checked.append(repo)
            return [], {"etag": None, "candidates": [], "latest_version": None}

        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "githubOwners.json")
            cache_path = os.path.join(directory, "githubOwnerCache.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"logins": ["example"]}, config_file)
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                json.dump(cache, cache_file)

            with mock.patch.object(mirror, "GITHUB_TOKEN", ""),                 mock.patch.object(mirror, "GITHUB_NEW_REPOSITORY_BUDGET", 2),                 mock.patch.object(
                    mirror, "_github_owner_repositories", return_value=([], {})
                ),                 mock.patch.object(
                    mirror, "_github_release_asset_state", side_effect=release_state
                ):
                mirror.fetch_github_owners(config_path, cache_path)

            with open(cache_path, "r", encoding="utf-8") as cache_file:
                written = json.load(cache_file)

        self.assertEqual(["example/a", "example/b"], sorted(checked))
        self.assertEqual(
            ["example/c"], written["__discovery__"]["pending_repositories"]
        )

    def test_rate_limited_first_time_check_waits_for_the_next_run(self):
        cache = {
            "__discovery__": {
                "addon_repositories": [],
                "scanned_repositories": ["example/new"],
                "pending_repositories": ["example/new"],
            }
        }

        def release_state(repo, previous_state=None):
            raise httpError(
                "https://api.github.invalid", 403, "rate limit"
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "githubOwners.json")
            cache_path = os.path.join(directory, "githubOwnerCache.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"logins": ["example"]}, config_file)
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                json.dump(cache, cache_file)

            with mock.patch.object(mirror, "GITHUB_TOKEN", ""),                 mock.patch.object(
                    mirror, "_github_owner_repositories", return_value=([], {})
                ),                 mock.patch.object(
                    mirror, "_github_release_asset_state", side_effect=release_state
                ):
                mirror.fetch_github_owners(config_path, cache_path)

            with open(cache_path, "r", encoding="utf-8") as cache_file:
                written = json.load(cache_file)

        self.assertEqual(
            ["example/new"], written["__discovery__"]["pending_repositories"]
        )

    def test_rate_limited_published_repository_still_stops_publication(self):
        cache = {
            "__discovery__": {
                "addon_repositories": ["example/published"],
                "scanned_repositories": ["example/published"],
                "release_state": {"example/published": {"etag": '"e"'}},
            }
        }

        def release_state(repo, previous_state=None):
            raise httpError(
                "https://api.github.invalid", 403, "rate limit"
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "githubOwners.json")
            cache_path = os.path.join(directory, "githubOwnerCache.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"logins": ["example"]}, config_file)
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                json.dump(cache, cache_file)

            with mock.patch.object(mirror, "GITHUB_TOKEN", ""),                 mock.patch.object(
                    mirror, "_github_owner_repositories", return_value=([], {})
                ),                 mock.patch.object(
                    mirror, "_github_release_asset_state", side_effect=release_state
                ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "refusing to publish incomplete GitHub author discovery",
                ):
                    mirror.fetch_github_owners(config_path, cache_path)

    def test_telegram_configuration_selects_original_owner(self):
        owners = mirror._load_github_owners()
        by_login = {spec["login"].casefold(): spec for spec in owners}

        self.assertIn("keyang556", by_login)
        self.assertEqual("exclude", by_login["serrebidev"]["fork_policy"])

    def test_requested_authors_stay_configured(self):
        # Requested by name. accessolutions is an organization rather than a
        # user, which repository discovery handles through its own GraphQL
        # branch, so it is worth pinning down alongside the two accounts.
        by_login = {
            spec["login"].casefold(): spec
            for spec in mirror._load_github_owners()
        }
        for login in ("javidominguez", "mltony", "accessolutions"):
            with self.subTest(login=login):
                self.assertIn(login, by_login)

    def test_every_author_requires_forks_to_be_newer_by_default(self):
        # "No forks unless the fork is newer" is the rule for every GitHub
        # source. Only serrebidev opts out, and it opts out the stricter way,
        # by dropping forks entirely.
        for spec in mirror._load_github_owners():
            with self.subTest(login=spec["login"]):
                self.assertIn(
                    spec.get("fork_policy", "newer-release-only"),
                    ("newer-release-only", "exclude"),
                )

    def test_owner_can_exclude_all_forks(self):
        response = {
            "owner0": {
                "repositories": {
                    "nodes": [
                        {
                            "nameWithOwner": "example/contribution",
                            "isFork": True,
                            "parent": {"nameWithOwner": "upstream/original"},
                        },
                        {"nameWithOwner": "example/original", "isFork": False},
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with mock.patch.object(mirror, "_github_graphql", return_value=response):
            repositories, fork_parents = mirror._github_owner_repository_names(
                [{"login": "example", "fork_policy": "exclude"}]
            )

        self.assertEqual({"example/original"}, repositories)
        self.assertEqual({}, fork_parents)

    def test_newer_fork_release_is_kept(self):
        candidate = {
            "repo": "example/forked",
            "asset_name": "example-2.0.nvda-addon",
            "release_tag": "v2.0",
        }
        kept, _states, rejected = mirror._filter_fork_candidates(
            [candidate],
            {"example/forked": "upstream/original"},
            {"upstream/original": {"latest_version": [1, 5, 0]}},
            {},
        )

        self.assertEqual([candidate], kept)
        self.assertEqual([], rejected)

    def test_equal_or_older_fork_release_is_rejected(self):
        candidate = {
            "repo": "example/forked",
            "asset_name": "example-1.5.nvda-addon",
            "release_tag": "v1.5",
        }
        kept, _states, rejected = mirror._filter_fork_candidates(
            [candidate],
            {"example/forked": "upstream/original"},
            {"upstream/original": {"latest_version": [1, 5, 0]}},
            {},
        )

        self.assertEqual([], kept)
        self.assertEqual(1, len(rejected))
        self.assertIn("is not newer", rejected[0]["reason"])

    def test_unchanged_release_reuses_conditional_cache(self):
        candidate = {
            "repo": "example/addon",
            "asset_name": "addon-1.0.nvda-addon",
        }
        state = {"etag": '"release-etag"', "candidates": [candidate]}
        with mock.patch.object(
            mirror,
            "_github_json_conditional",
            return_value=(None, '"release-etag"', True),
        ):
            candidates, updated_state = mirror._github_release_asset_state(
                "example/addon",
                state,
            )

        self.assertEqual([candidate], candidates)
        self.assertIs(state, updated_state)

    def test_asset_families_ignore_version_suffixes(self):
        self.assertEqual("brailab", mirror._asset_family("brailab-3.1.5.nvda-addon"))
        self.assertEqual(
            "pctalker_pc_speaker_hungarian_demo",
            mirror._asset_family(
                "PCTALKER_PC_SPEAKER_Hungarian_DEMO_0.2.5.1.nvda-addon"
            ),
        )
        self.assertEqual(
            "tgspeechbox",
            mirror._asset_family("TGSpeechBox-v310b802.nvda-addon"),
        )

    def test_multiple_addon_families_survive_one_repository(self):
        releases = [
            {
                "isDraft": False,
                "isPrerelease": False,
                "tagName": "v2",
                "releaseAssets": {
                    "nodes": [
                        {"name": "brailab-3.1.5.nvda-addon", "downloadUrl": "https://a"},
                        {"name": "brailabEmulated-3.2.2.nvda-addon", "downloadUrl": "https://b"},
                        {"name": "readme.txt", "downloadUrl": "https://c"},
                    ]
                },
            }
        ]
        candidates = mirror._release_asset_candidates_from_records(
            "example/Brailab", releases
        )

        self.assertEqual(2, len(candidates))
        self.assertEqual(
            {"brailab-3.1.5.nvda-addon", "brailabEmulated-3.2.2.nvda-addon"},
            {candidate["asset_name"] for candidate in candidates},
        )

    def test_bundle_requires_root_manifest(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("not-an-addon.txt", "no manifest")
        candidate = {
            "repo": "example/not-an-addon",
            "channel": "stable",
            "asset_name": "fake.nvda-addon",
            "download_url": "https://example.invalid/fake.nvda-addon",
            "release_tag": "v1",
            "published_at": None,
            "changelog": "",
        }

        with mock.patch.object(mirror, "http_get", return_value=stream.getvalue()):
            with self.assertRaises(KeyError):
                mirror._github_asset_entry(candidate)


class BestMidiTests(unittest.TestCase):
    def test_newer_asset_filename_repairs_stale_catalog_version(self):
        source = {
            "addons": [
                {
                    "name": "edgeReader",
                    "version": "1.2.4",
                    "download_name": "edgeReader-1.2.6.nvda-addon",
                    "download_url": "https://example.invalid/edgeReader-1.2.6.nvda-addon",
                }
            ]
        }
        with mock.patch.object(mirror, "http_get_json", return_value=source):
            self.assertEqual("1.2.6", mirror.fetch_bestmidi()[0]["version"])


class TranslationTests(unittest.TestCase):
    def test_edge_reader_description_is_english(self):
        translations = mirror.load_translations()
        description = translations["edgeReader"]["description"]

        self.assertIn("automatically saves any text", description)
        self.assertNotRegex(description, r"[\u0400-\u04ff]")

    def test_calendar_changelog_is_english(self):
        translations = mirror.load_translations()
        self.assertEqual(
            "Release notes are not available in English.",
            translations["Calendar"]["changelog"],
        )

    def test_untranslated_release_notes_get_english_fallback(self):
        old_translations = mirror.TRANSLATIONS
        mirror.TRANSLATIONS = {}
        entry = {
            "name": "exampleAddon",
            "changelog": "Исправлена ошибка.",
        }
        try:
            mirror._translate_entry(entry)
        finally:
            mirror.TRANSLATIONS = old_translations

        self.assertEqual(
            "Release notes are not available in English.",
            entry["changelog"],
        )


    def test_english_release_notes_survive(self):
        """English notes were wiped by "version" and "correct" matching Spanish.

        ``versi[oo]n`` matched plain "Version" and ``corre[cc][a-z]*`` matched
        "corrected", so hundreds of add-ons that did publish English release
        notes reached the store claiming they had none.
        """
        old_translations = mirror.TRANSLATIONS
        mirror.TRANSLATIONS = {}
        try:
            for changelog in (
                "Version 1.2: added a browse mode command.",
                "Corrected a crash when the list was empty.",
                "Bug corrections and a new setting.",
                "This corrects the reading order of tables.",
                "Added support for newer versions of NVDA.",
            ):
                entry = {"name": "exampleAddon", "changelog": changelog}
                mirror._translate_entry(entry)
                self.assertEqual(changelog, entry["changelog"])
        finally:
            mirror.TRANSLATIONS = old_translations

    def test_non_english_release_notes_are_still_detected(self):
        old_translations = mirror.TRANSLATIONS
        mirror.TRANSLATIONS = {}
        try:
            for changelog in (
                "Versi\u00f3n 1.2 publicada.",
                "Correcci\u00f3n de errores.",
                "Corre\u00e7\u00e3o de bugs e melhorias.",
                "A\u00f1adido soporte para tablas.",
                "Yeni s\u00fcr\u00fcm eklendi.",
                "Perbaikan bug.",
            ):
                entry = {"name": "exampleAddon", "changelog": changelog}
                mirror._translate_entry(entry)
                self.assertEqual(
                    mirror.CHANGELOG_UNAVAILABLE,
                    entry["changelog"],
                    changelog,
                )
        finally:
            mirror.TRANSLATIONS = old_translations

    def test_english_notes_are_borrowed_from_another_source(self):
        """The same add-on reaches the mirror twice, in two languages.

        nvda-addons.ru republishes hundreds of official add-ons in its own
        "Dev" channel with Russian notes. dedupe keys on (addonId, channel),
        so the two never meet and the Russian entry used to be published as
        having no English release notes at all.
        """
        entries = [
            {
                "name": "exampleAddon",
                "source": "official",
                "channel": "stable",
                "changelog": "Added a browse mode command.",
            },
            {
                "name": "exampleAddon",
                "source": "ru",
                "channel": "dev",
                "changelog": "\u0418\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0430 \u043e\u0448\u0438\u0431\u043a\u0430.",
            },
        ]
        old_translations = mirror.TRANSLATIONS
        old_index = mirror.ENGLISH_CHANGELOGS
        mirror.TRANSLATIONS = {}
        mirror.ENGLISH_CHANGELOGS = mirror.english_changelogs(entries)
        try:
            russian = dict(entries[1])
            mirror._translate_entry(russian)
            self.assertEqual("Added a browse mode command.", russian["changelog"])
        finally:
            mirror.TRANSLATIONS = old_translations
            mirror.ENGLISH_CHANGELOGS = old_index

    def test_borrowed_notes_prefer_the_more_trusted_source(self):
        entries = [
            {
                "name": "exampleAddon",
                "source": "bestmidi",
                "channel": "stable",
                "changelog": "Repository description.",
            },
            {
                "name": "exampleAddon",
                "source": "official",
                "channel": "stable",
                "changelog": "Reviewed release notes.",
            },
        ]
        self.assertEqual(
            {"exampleAddon": "Reviewed release notes."},
            mirror.english_changelogs(entries),
        )

    def test_borrowing_never_offers_non_english_notes(self):
        entries = [
            {
                "name": "exampleAddon",
                "source": "official",
                "channel": "stable",
                "changelog": "Correcci\u00f3n de errores.",
            },
        ]
        self.assertEqual({}, mirror.english_changelogs(entries))


class UnparseableOfficialVersionTests(unittest.TestCase):
    def test_official_entry_without_a_numeric_version_does_not_abort_the_build(self):
        """reject_reason trusts official versions without parsing them.

        transform() then indexed sanitize_version() unconditionally, so a
        single malformed official version would have raised TypeError and
        taken the whole publication with it.
        """
        entry = {
            "name": "exampleAddon",
            "summary": "Example",
            "description": "",
            "author": "Example",
            "version": "beta",
            "channel": "stable",
            "source": "official",
            "download_url": "https://example.invalid/example.nvda-addon",
        }
        obj = mirror.transform(entry, "0" * 64)

        self.assertEqual("beta", obj["addonVersionName"])
        self.assertEqual(
            {"major": 0, "minor": 0, "patch": 0},
            obj["addonVersionNumber"],
        )


class ChannelDuplicateTests(unittest.TestCase):
    def _entry(self, channel, version, source="ru"):
        return {
            "name": "exampleAddon",
            "channel": channel,
            "version": version,
            "source": source,
        }

    def test_dev_row_with_the_stable_version_is_dropped(self):
        entries = [
            self._entry("stable", "1.8", source="github_owner"),
            self._entry("dev", "1.8"),
        ]
        kept, rejected = mirror.drop_redundant_channel_duplicates(entries)

        self.assertEqual(["stable"], [entry["channel"] for entry in kept])
        self.assertEqual(1, len(rejected))
        self.assertEqual("exampleAddon", rejected[0]["addonId"])
        self.assertIn("stable channel", rejected[0]["reason"])
        self.assertIn("dev", rejected[0]["reason"])

    def test_a_genuinely_newer_dev_row_is_kept(self):
        entries = [
            self._entry("stable", "1.8", source="github_owner"),
            self._entry("dev", "1.9"),
        ]
        kept, rejected = mirror.drop_redundant_channel_duplicates(entries)

        self.assertEqual(["stable", "dev"], [entry["channel"] for entry in kept])
        self.assertEqual([], rejected)

    def test_the_same_release_spelled_differently_still_collapses(self):
        """NVDA compares the numbers, so the mirror must too.

        The official store publishes "2026.05.03" where nvda-addons.ru
        republishes the identical release as "2026.5.3".
        """
        entries = [
            self._entry("stable", "2026.05.03", source="official"),
            self._entry("dev", "2026.5.3"),
        ]
        kept, _rejected = mirror.drop_redundant_channel_duplicates(entries)

        self.assertEqual(["stable"], [entry["channel"] for entry in kept])

    def test_beta_matching_stable_collapses_too(self):
        entries = [
            self._entry("stable", "3.0", source="official"),
            self._entry("beta", "3.0", source="official"),
        ]
        kept, rejected = mirror.drop_redundant_channel_duplicates(entries)

        self.assertEqual(["stable"], [entry["channel"] for entry in kept])
        self.assertIn("beta", rejected[0]["reason"])

    def test_a_dev_only_add_on_survives(self):
        entries = [self._entry("dev", "0.4")]
        kept, rejected = mirror.drop_redundant_channel_duplicates(entries)

        self.assertEqual(entries, kept)
        self.assertEqual([], rejected)

    def test_other_add_ons_do_not_suppress_each_other(self):
        entries = [
            {"name": "one", "channel": "stable", "version": "2.0", "source": "official"},
            {"name": "two", "channel": "dev", "version": "2.0", "source": "ru"},
        ]
        kept, rejected = mirror.drop_redundant_channel_duplicates(entries)

        self.assertEqual(entries, kept)
        self.assertEqual([], rejected)

    def test_matching_is_case_insensitive_like_dedupe(self):
        entries = [
            {"name": "ExampleAddon", "channel": "stable", "version": "1.0", "source": "official"},
            {"name": "exampleaddon", "channel": "dev", "version": "1.0", "source": "ru"},
        ]
        kept, rejected = mirror.drop_redundant_channel_duplicates(entries)

        self.assertEqual(["ExampleAddon"], [entry["name"] for entry in kept])
        self.assertEqual(1, len(rejected))


class HelperSafetyTests(unittest.TestCase):
    def test_helper_does_not_replace_nvda_data_manager_singleton(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "helper",
            "globalPlugins",
            "addonStoreMirror.py",
        )
        with open(path, "r", encoding="utf-8") as helper_file:
            source = helper_file.read()

        self.assertNotIn("dataManager.initialize()", source)
        self.assertIn("dataManager.addonDataManager", source)


if __name__ == "__main__":
    unittest.main()
