"""Tests for the add-on bundle export/import module (addonStoreBundles)."""
import builtins
import hashlib
import importlib.util
import itertools
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


BUNDLES_PATH = (
    Path(__file__).resolve().parents[1]
    / "helper"
    / "globalPlugins"
    / "_addonStoreBundles.py"
)


class _WxModule(types.ModuleType):
    """Fake wx: every attribute is a unique int, like the helper tests use."""

    def __init__(self, name):
        super().__init__(name)
        self._ids = itertools.count(1)

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        value = next(self._ids)
        setattr(self, name, value)
        return value


def _loadBundles():
    wxModule = _WxModule("wx")

    class _FakeDialog:
        def __init__(self, parent=None, title=""):
            self.parent = parent
            self.title = title

    wxModule.Dialog = _FakeDialog
    with mock.patch.dict(sys.modules, {"wx": wxModule}):
        spec = importlib.util.spec_from_file_location("addonStoreBundles", BUNDLES_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


bundles = _loadBundles()


class _FakeResponse:
    def __init__(self, payload, contentLength=None):
        self._payload = payload
        self.headers = {"Content-Length": str(contentLength if contentLength is not None else len(payload))}

    def read(self, size=-1):
        if size is None or size < 0:
            data, self._payload = self._payload, b""
            return data
        data, self._payload = self._payload[:size], self._payload[size:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _catalogJson():
    return json.dumps({
        "addons": {
            "alpha": {
                "addonId": "alpha",
                "displayName": "Alpha",
                "addonVersionName": "2.0",
                "URL": "https://example.com/alpha-2.0.nvda-addon",
                "sha256": "a" * 64,
            },
            "beta": {
                "addonId": "beta",
                "displayName": "Beta",
                "addonVersionName": "1.5",
                "URL": "https://example.com/beta-1.5.nvda-addon",
                "sha256": "",
            },
        },
    }).encode("utf-8")


class BundleFormatTests(unittest.TestCase):
    def _translate(self):
        patcher = mock.patch.object(builtins, "_", lambda text: text, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def setUp(self):
        self._translate()

    def test_module_name_is_loader_safe(self):
        # NVDA's globalPluginHandler skips modules whose names start with "_";
        # the bundle helper must keep that prefix or NVDA logs an import error.
        self.assertTrue(BUNDLES_PATH.name.startswith("_"))
        self.assertTrue(BUNDLES_PATH.is_file())

    def test_build_parse_round_trip(self):
        entries = [
            bundles.makeEntry(
                addonId="alpha", displayName="Alpha", installedVersion="2.0",
                mode=bundles.MODE_PINNED, version="2.0",
                url="https://example.com/alpha-2.0.nvda-addon", sha256="b" * 64,
            ),
            bundles.makeEntry(
                addonId="beta", displayName="Beta", installedVersion="1.5",
                mode=bundles.MODE_LATEST,
            ),
        ]
        bundle = bundles.buildBundle("My bundle", entries)
        self.assertEqual("nvda-addon-bundle", bundle["format"])
        self.assertEqual(1, bundle["formatVersion"])
        self.assertEqual("My bundle", bundle["name"])
        self.assertEqual(2, len(bundle["addons"]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.nvda-bundle")
            bundles.saveBundleFile(bundle, path)
            loaded = bundles.loadBundleFile(path)
        self.assertEqual(bundle, loaded)

    def test_parse_rejects_bad_documents(self):
        bad = [
            {"format": "something-else", "formatVersion": 1, "addons": []},
            {"format": "nvda-addon-bundle", "formatVersion": 99, "addons": [{"addonId": "x"}]},
            {"format": "nvda-addon-bundle", "formatVersion": 1, "addons": []},
            {"format": "nvda-addon-bundle", "formatVersion": 1},
            {"format": "nvda-addon-bundle", "formatVersion": 1,
             "addons": [{"displayName": "No id"}]},
            {"format": "nvda-addon-bundle", "formatVersion": 1,
             "addons": [{"addonId": "x", "mode": "pinned", "version": "1.0"}]},
            ["not", "a", "dict"],
        ]
        for doc in bad:
            with self.assertRaises(bundles.BundleError, msg=repr(doc)):
                bundles.parseBundle(doc)

    def test_makeEntry_rejects_bad_modes(self):
        with self.assertRaises(ValueError):
            bundles.makeEntry(
                addonId="x", displayName="X", installedVersion="1.0", mode="frozen",
            )
        with self.assertRaises(ValueError):
            bundles.makeEntry(
                addonId="x", displayName="X", installedVersion="1.0",
                mode=bundles.MODE_PINNED, version="1.0", url="",
            )

    def test_loadBundleFile_missing_and_corrupt(self):
        with self.assertRaises(bundles.BundleError):
            bundles.loadBundleFile(os.path.join(tempfile.gettempdir(), "no-such-bundle-xyz.nvda-bundle"))
        with tempfile.NamedTemporaryFile("w", suffix=".nvda-bundle", delete=False) as f:
            f.write("{ not json")
            path = f.name
        try:
            with self.assertRaises(bundles.BundleError):
                bundles.loadBundleFile(path)
        finally:
            os.remove(path)


class CatalogTests(unittest.TestCase):
    def test_fetchCatalogMap(self):
        payload = _catalogJson()

        def fakeUrlopen(url, timeout=None):
            self.assertIn("addons.json", url)
            return _FakeResponse(payload)

        catalog = bundles.fetchCatalogMap(urlopen=fakeUrlopen)
        self.assertEqual("2.0", catalog["alpha"]["version"])
        self.assertEqual("https://example.com/alpha-2.0.nvda-addon", catalog["alpha"]["url"])
        self.assertEqual("a" * 64, catalog["alpha"]["sha256"])
        self.assertEqual("Beta", catalog["beta"]["displayName"])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.catalog = bundles.fetchCatalogMap(
            urlopen=lambda url, timeout=None: _FakeResponse(_catalogJson()),
        )
        self.installed = [
            {"addonId": "alpha", "displayName": "Alpha", "version": "2.0"},
            {"addonId": "beta", "displayName": "Beta", "version": "1.4"},
            {"addonId": "gamma", "displayName": "Gamma", "version": "3.0"},
        ]

    def test_pin_matching_versions(self):
        entries = bundles.buildExportEntries(self.installed, self.catalog, pinVersions=True)
        byId = {e["addonId"]: e for e in entries}
        alpha = byId["alpha"]
        self.assertEqual(bundles.MODE_PINNED, alpha["mode"])
        self.assertEqual("2.0", alpha["version"])
        self.assertEqual("https://example.com/alpha-2.0.nvda-addon", alpha["url"])
        self.assertEqual("a" * 64, alpha["sha256"])

    def test_pin_falls_back_to_latest(self):
        entries = bundles.buildExportEntries(self.installed, self.catalog, pinVersions=True)
        byId = {e["addonId"]: e for e in entries}
        # beta 1.4 installed, catalog has 1.5: cannot pin the installed version.
        self.assertEqual(bundles.MODE_LATEST, byId["beta"]["mode"])
        self.assertNotIn("url", byId["beta"])
        # gamma is not in the catalog at all.
        self.assertEqual(bundles.MODE_LATEST, byId["gamma"]["mode"])

    def test_latest_mode_never_pins(self):
        entries = bundles.buildExportEntries(self.installed, self.catalog, pinVersions=False)
        for entry in entries:
            self.assertEqual(bundles.MODE_LATEST, entry["mode"])
            self.assertNotIn("url", entry)

    def test_getInstalledAddons_sorted(self):
        def makeAddon(name, summary, version):
            addon = types.SimpleNamespace()
            addon.name = name
            addon.version = version
            addon.manifest = {"summary": summary}
            return addon

        handler = types.SimpleNamespace(
            getAvailableAddons=lambda: iter([
                makeAddon("zeta", "Zeta", "1.0"),
                makeAddon("alpha", "Alpha", "2.0"),
            ]),
        )
        installed = bundles.getInstalledAddons(handler)
        self.assertEqual(["alpha", "zeta"], [i["addonId"] for i in installed])
        self.assertEqual("Alpha", installed[0]["displayName"])


class ResolveTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(builtins, "_", lambda text: text, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.catalog = bundles.fetchCatalogMap(
            urlopen=lambda url, timeout=None: _FakeResponse(_catalogJson()),
        )

    def _bundle(self, entries):
        return bundles.buildBundle("Test", entries)

    def test_latest_resolves_from_catalog(self):
        bundle = self._bundle([
            bundles.makeEntry(addonId="alpha", displayName="Alpha",
                              installedVersion="1.0", mode=bundles.MODE_LATEST),
        ])
        resolved = bundles.resolveEntries(bundle, self.catalog, {})
        self.assertEqual(1, len(resolved))
        item = resolved[0]
        self.assertEqual("2.0", item["version"])
        self.assertEqual("https://example.com/alpha-2.0.nvda-addon", item["url"])
        self.assertEqual("new", item["status"])

    def test_pinned_uses_its_own_url(self):
        bundle = self._bundle([
            bundles.makeEntry(addonId="alpha", displayName="Alpha",
                              installedVersion="1.0", mode=bundles.MODE_PINNED,
                              version="1.0", url="https://example.com/alpha-1.0.nvda-addon",
                              sha256="c" * 64),
        ])
        resolved = bundles.resolveEntries(bundle, self.catalog, {})
        item = resolved[0]
        self.assertEqual("1.0", item["version"])
        self.assertEqual("https://example.com/alpha-1.0.nvda-addon", item["url"])

    def test_unknown_addon_unavailable(self):
        bundle = self._bundle([
            bundles.makeEntry(addonId="nope", displayName="Nope",
                              installedVersion="1.0", mode=bundles.MODE_LATEST),
        ])
        resolved = bundles.resolveEntries(bundle, self.catalog, {})
        self.assertEqual("unavailable", resolved[0]["status"])
        self.assertEqual("", resolved[0]["url"])

    def test_status_against_installed(self):
        bundle = self._bundle([
            bundles.makeEntry(addonId="alpha", displayName="Alpha",
                              installedVersion="2.0", mode=bundles.MODE_LATEST),
            bundles.makeEntry(addonId="beta", displayName="Beta",
                              installedVersion="1.5", mode=bundles.MODE_LATEST),
        ])
        installedMap = {
            "alpha": {"addonId": "alpha", "displayName": "Alpha", "version": "2.0"},
            "beta": {"addonId": "beta", "displayName": "Beta", "version": "1.4"},
        }
        resolved = bundles.resolveEntries(bundle, self.catalog, installedMap)
        byId = {r["addonId"]: r for r in resolved}
        self.assertEqual("up-to-date", byId["alpha"]["status"])
        self.assertEqual("update", byId["beta"]["status"])
        self.assertEqual("1.4", byId["beta"]["installedVersion"])


class DownloadTests(unittest.TestCase):
    def test_downloadToTemp_writes_bytes(self):
        payload = b"fake-addon-bytes"
        seen = []

        def fakeUrlopen(url, timeout=None):
            seen.append(url)
            return _FakeResponse(payload)

        progress = []
        path = bundles.downloadToTemp(
            "https://example.com/x.nvda-addon",
            progress=lambda done, total: progress.append((done, total)),
            urlopen=fakeUrlopen,
        )
        try:
            with open(path, "rb") as f:
                self.assertEqual(payload, f.read())
            self.assertTrue(path.endswith(".nvda-addon"))
            self.assertEqual(["https://example.com/x.nvda-addon"], seen)
            self.assertTrue(progress)
            self.assertEqual((len(payload), len(payload)), progress[-1])
        finally:
            os.remove(path)

    def test_downloadToTemp_cleans_up_on_error(self):
        def fakeUrlopen(url, timeout=None):
            raise OSError("no network")

        with self.assertRaises(OSError):
            bundles.downloadToTemp("https://example.com/x.nvda-addon", urlopen=fakeUrlopen)

    def test_sha256OfFile(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name
        try:
            self.assertEqual(hashlib.sha256(b"abc").hexdigest(), bundles.sha256OfFile(path))
        finally:
            os.remove(path)


class InstallTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(builtins, "_", lambda text: text, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _resolved(self, **overrides):
        item = {
            "addonId": "alpha",
            "displayName": "Alpha",
            "version": "2.0",
            "url": "https://example.com/alpha-2.0.nvda-addon",
            "sha256": "",
            "source": "example.com",
            "status": "new",
            "installedVersion": "",
        }
        item.update(overrides)
        return item

    def test_installs_all(self):
        installedPaths = []

        def fakeDownloader(url):
            fd, path = tempfile.mkstemp(suffix=".nvda-addon")
            os.close(fd)
            with open(path, "wb") as f:
                f.write(b"data")
            return path

        def fakeInstaller(path):
            installedPaths.append(path)
            self.assertTrue(os.path.exists(path))

        progress = []
        result = bundles.installResolved(
            [self._resolved(), self._resolved(addonId="beta", displayName="Beta")],
            downloader=fakeDownloader,
            installer=fakeInstaller,
            progress=lambda *args: progress.append(args),
        )
        self.assertEqual(["Alpha", "Beta"], result["installed"])
        self.assertEqual([], result["failed"])
        self.assertEqual(2, len(installedPaths))
        self.assertTrue(progress)
        # Temp downloads are cleaned up after install.
        for path in installedPaths:
            self.assertFalse(os.path.exists(path))

    def test_checksum_mismatch_fails(self):
        installed = []

        def fakeDownloader(url):
            fd, path = tempfile.mkstemp(suffix=".nvda-addon")
            os.close(fd)
            with open(path, "wb") as f:
                f.write(b"tampered")
            return path

        result = bundles.installResolved(
            [self._resolved(sha256="0" * 64)],
            downloader=fakeDownloader,
            installer=installed.append,
        )
        self.assertEqual([], result["installed"])
        self.assertEqual([], installed)
        self.assertEqual(1, len(result["failed"]))
        self.assertIn("Checksum", result["failed"][0][1])

    def test_download_failure_recorded(self):
        def fakeDownloader(url):
            raise OSError("offline")

        result = bundles.installResolved(
            [self._resolved()],
            downloader=fakeDownloader,
            installer=lambda path: None,
        )
        self.assertEqual([], result["installed"])
        self.assertEqual(1, len(result["failed"]))
        self.assertIn("Download failed", result["failed"][0][1])

    def test_installer_exception_recorded(self):
        def fakeDownloader(url):
            fd, path = tempfile.mkstemp(suffix=".nvda-addon")
            os.close(fd)
            return path

        def fakeInstaller(path):
            raise RuntimeError("broken manifest")

        result = bundles.installResolved(
            [self._resolved()],
            downloader=fakeDownloader,
            installer=fakeInstaller,
        )
        self.assertEqual([], result["installed"])
        self.assertEqual([("Alpha", "broken manifest")], result["failed"])


class DowngradeStatusTests(unittest.TestCase):
    def test_older_bundle_version_is_not_an_update(self):
        bundle = bundles.buildBundle("b", [
            bundles.makeEntry(addonId="alpha", displayName="Alpha", installedVersion="1.0",
                              mode=bundles.MODE_PINNED, version="1.0",
                              url="https://example.org/a.nvda-addon"),
        ])
        installedMap = {"alpha": {"addonId": "alpha", "displayName": "Alpha", "version": "2.1"}}
        [item] = bundles.resolveEntries(bundle, {}, installedMap)
        self.assertEqual("older", item["status"])

    def test_unparseable_versions_still_count_as_update(self):
        bundle = bundles.buildBundle("b", [
            bundles.makeEntry(addonId="alpha", displayName="Alpha", installedVersion="x",
                              mode=bundles.MODE_PINNED, version="nightly",
                              url="https://example.org/a.nvda-addon"),
        ])
        installedMap = {"alpha": {"addonId": "alpha", "displayName": "Alpha", "version": "2.1"}}
        [item] = bundles.resolveEntries(bundle, {}, installedMap)
        self.assertEqual("update", item["status"])


class EmbeddedBundleTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(builtins, "_", lambda text: text, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_packInstalledAddon_zips_manifest_at_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "manifest.ini"), "w", encoding="utf-8") as f:
                f.write("name = reloqC\nversion = 1.1.2\n")
            os.makedirs(os.path.join(tmp, "synthDrivers"))
            with open(os.path.join(tmp, "synthDrivers", "reloq.py"), "w") as f:
                f.write("# driver\n")
            data = bundles.packInstalledAddon(tmp)
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIn("manifest.ini", z.namelist())
            self.assertIn("synthDrivers/reloq.py", z.namelist())

    def test_embedded_round_trip_carries_exact_bytes(self):
        payload = b"unreleased-addon-bytes"
        installed = [{"addonId": "reloqC", "displayName": "Eloquence C", "version": "1.1.2", "path": "/x"}]
        entries, files = bundles.buildEmbeddedEntries(installed, packer=lambda addon: payload)
        self.assertEqual(bundles.MODE_EMBEDDED, entries[0]["mode"])
        self.assertEqual("1.1.2", entries[0]["version"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), entries[0]["sha256"])
        bundle = bundles.buildBundle("Share", entries)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "share.nvda-bundle")
            bundles.saveBundleFile(bundle, path, embeddedFiles=files)
            loaded = bundles.loadBundleFile(path)
        [entry] = loaded["addons"]
        self.assertEqual(payload, entry["_data"])
        resolved = bundles.resolveEntries(loaded, {}, {})
        self.assertEqual("new", resolved[0]["status"])
        self.assertEqual(payload, resolved[0]["embeddedData"])

    def test_embedded_install_uses_embedded_bytes_not_network(self):
        payload = b"unreleased-addon-bytes"
        seen = []

        def fakeInstaller(path):
            with open(path, "rb") as f:
                seen.append(f.read())

        item = {
            "addonId": "reloqC", "displayName": "Eloquence C", "version": "1.1.2",
            "url": "", "sha256": hashlib.sha256(payload).hexdigest(),
            "source": "", "status": "up-to-date", "installedVersion": "1.1.2",
            "embeddedData": payload,
        }
        result = bundles.installResolved(
            [item],
            downloader=lambda url: (_ for _ in ()).throw(AssertionError("must not download")),
            installer=fakeInstaller,
        )
        self.assertEqual(["Eloquence C"], result["installed"])
        self.assertEqual([payload], seen)

    def test_plain_bundle_still_saves_as_json(self):
        bundle = bundles.buildBundle("Plain", [
            bundles.makeEntry(addonId="a", displayName="A", installedVersion="1.0", mode=bundles.MODE_LATEST),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plain.nvda-bundle")
            bundles.saveBundleFile(bundle, path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(bundle, json.load(f))
            self.assertEqual(bundle, bundles.loadBundleFile(path))


class ReinstallPlanTests(unittest.TestCase):
    def _item(self, addonId, status, installedVersion="", version=""):
        return {
            "addonId": addonId, "displayName": addonId, "version": version or installedVersion,
            "url": "https://example.com/x.nvda-addon", "sha256": "", "source": "example.com",
            "status": status, "installedVersion": installedVersion,
        }

    def test_same_version_needs_confirmation_and_is_dropped_without_it(self):
        items = [self._item("alpha", "up-to-date", "2.0"), self._item("beta", "new")]
        self.assertEqual(["alpha"], [i["addonId"] for i in bundles.reinstallItems(items)])
        self.assertEqual(["beta"], [i["addonId"] for i in bundles.planInstall(items, reinstallConfirmed=False)])

    def test_same_version_installs_when_confirmed(self):
        items = [self._item("alpha", "up-to-date", "2.0"), self._item("beta", "new")]
        self.assertEqual(["alpha", "beta"], [i["addonId"] for i in bundles.planInstall(items, reinstallConfirmed=True)])


class RunPumpedTests(unittest.TestCase):
    """Network work must run through NVDA's ExecAndPump, not on the GUI thread."""

    def test_uses_exec_and_pump_inside_nvda(self):
        calls = []

        class ExecAndPump:
            def __init__(self, func, *args, **kwargs):
                calls.append((func, args, kwargs))
                self.funcRes = func(*args, **kwargs)

        systemUtils = types.ModuleType("systemUtils")
        systemUtils.ExecAndPump = ExecAndPump
        with mock.patch.dict(sys.modules, {"systemUtils": systemUtils}):
            result = bundles.runPumped(lambda value: value * 2, 21)
        self.assertEqual(42, result)
        self.assertEqual(1, len(calls))

    def test_runs_directly_outside_nvda(self):
        with mock.patch.dict(sys.modules, {"systemUtils": None}):
            self.assertEqual(3, bundles.runPumped(lambda: 3))


if __name__ == "__main__":
    unittest.main()
