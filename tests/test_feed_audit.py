import json
import os
import tempfile
import unittest

import mirror


def entry(
    addon_id="ExampleAddon",
    channel="stable",
    min_v=(2025, 1, 0),
    last_v=(2026, 1, 0),
    **overrides,
):
    """A structurally valid NVDA add-on store object for audit tests."""
    base = {
        "addonId": addon_id,
        "displayName": "Example",
        "description": "Example description.",
        "publisher": "Example Author",
        "channel": channel,
        "addonVersionName": "1.0",
        "addonVersionNumber": {"major": 1, "minor": 0, "patch": 0},
        "license": "GPL-2.0",
        "licenseURL": "",
        "sourceURL": "https://example.invalid",
        "URL": "https://example.invalid/example.nvda-addon",
        "sha256": "a" * 64,
        "minNVDAVersion": dict(zip(("major", "minor", "patch"), min_v)),
        "lastTestedVersion": dict(zip(("major", "minor", "patch"), last_v)),
        "legacy": False,
    }
    base.update(overrides)
    return base


def build_output(
    directory,
    versions=None,
    counts=None,
    floors=None,
    extra_files=None,
    with_stats=True,
):
    """Write a minimal mirror output tree under directory and return its path."""
    out = os.path.join(directory, "public")
    os.makedirs(os.path.join(out, "en", "all"), exist_ok=True)
    if with_stats:
        stats = {
            "back_compat_to": floors or {"2026.2.0": [2026, 1, 0]},
            "compatible_counts": counts or {},
        }
        with open(os.path.join(out, "stats.json"), "w", encoding="utf-8") as f:
            json.dump(stats, f)
    for ver_name, entries in (versions or {}).items():
        path = os.path.join(out, "en", "all", f"{ver_name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f)
    for rel, content in (extra_files or {}).items():
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(content, f)
    return out


class AuditCatalogTests(unittest.TestCase):
    def test_clean_catalog_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(
                directory,
                versions={"2026.2.0": [entry()]},
                counts={"2026.2.0": 1},
            )
            self.assertEqual([], mirror.audit_catalog(out))

    def test_last_tested_below_back_compat_to_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(
                directory,
                versions={"2026.2.0": [entry(last_v=(2025, 1, 0))]},
            )
            problems = mirror.audit_catalog(out)
            self.assertEqual(1, len(problems))
            self.assertIn("BACK_COMPAT_TO", problems[0])

    def test_minimum_newer_than_api_version_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(
                directory,
                versions={"2026.1.0": [entry(min_v=(2026, 2, 0))]},
                floors={"2026.1.0": [2026, 1, 0]},
            )
            problems = mirror.audit_catalog(out)
            self.assertEqual(1, len(problems))
            self.assertIn("minimumNVDAVersion", problems[0])

    def test_missing_required_key_is_flagged(self):
        addon = entry()
        del addon["URL"]
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(directory, versions={"2026.2.0": [addon]})
            problems = mirror.audit_catalog(out)
            self.assertEqual(1, len(problems))
            self.assertIn("required key", problems[0])

    def test_unexpected_channel_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(
                directory,
                versions={"2026.2.0": [entry(channel="old")]},
            )
            problems = mirror.audit_catalog(out)
            self.assertEqual(1, len(problems))
            self.assertIn("channel", problems[0])

    def test_duplicate_addon_channel_row_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(
                directory,
                versions={"2026.2.0": [entry(), entry()]},
                counts={"2026.2.0": 2},
            )
            problems = mirror.audit_catalog(out)
            self.assertEqual(1, len(problems))
            self.assertIn("duplicate", problems[0])

    def test_count_mismatch_with_stats_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(
                directory,
                versions={"2026.2.0": [entry("A"), entry("B")]},
                counts={"2026.2.0": 1},
            )
            problems = mirror.audit_catalog(out)
            self.assertEqual(1, len(problems))
            self.assertIn("stats.json records", problems[0])

    def test_latest_json_is_not_audited(self):
        # The "show all (incompatible)" view legitimately holds old add-ons.
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(
                directory,
                versions={"2026.2.0": [entry()]},
                extra_files={
                    "en/all/latest.json": [entry(last_v=(1999, 1, 0))],
                },
            )
            self.assertEqual([], mirror.audit_catalog(out))

    def test_missing_stats_json_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(directory, with_stats=False)
            problems = mirror.audit_catalog(out)
            self.assertEqual(1, len(problems))
            self.assertIn("stats.json missing", problems[0])

    def test_unknown_api_version_without_floor_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = build_output(
                directory,
                versions={"2099.1.0": [entry()]},
                floors={},
            )
            problems = mirror.audit_catalog(out)
            self.assertEqual(1, len(problems))
            self.assertIn("BACK_COMPAT_TO", problems[0])


if __name__ == "__main__":
    unittest.main()