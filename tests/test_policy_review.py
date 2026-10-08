"""Independent regression checks for source-aware Add-on Store routing."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import tests.test_helper as helperTests


_POLICY_PATH = Path(__file__).parents[1] / "helper" / "globalPlugins" / "_addonStorePolicy.py"
_SPEC = importlib.util.spec_from_file_location("policy_review", _POLICY_PATH)
policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(policy)


class PolicyReviewTests(unittest.TestCase):
    def test_policy_installs_before_global_source_changes_and_preserves_empty_original(self):
        fixture = helperTests.HelperSourceSupportTests()
        helper = fixture._loadHelper({})
        fixture.config.conf["addonStore"]["baseServerURL"] = helper.MIRROR_STORE_URL
        fixture.config.conf["serrebiStore"] = {
            "originalStoreURL": "", "searchAsYouType": True, "storePolicy": "official",
        }
        seen = []
        globalVars = types.SimpleNamespace(appArgs=types.SimpleNamespace(secure=False))
        globalPlugins = types.ModuleType("globalPlugins")
        globalPlugins.__path__ = []
        with mock.patch.dict(sys.modules, {
            "globalVars": globalVars, "globalPlugins": globalPlugins,
            "globalPlugins._addonStorePolicy": policy,
        }), \
            mock.patch.object(helper.GlobalPlugin, "_enableStorePolicy", lambda self: (
                seen.append(fixture.config.conf["addonStore"]["baseServerURL"]) or True
            )), \
            mock.patch.object(helper.GlobalPlugin, "_removeStaleBundleModule"), \
            mock.patch.object(helper.GlobalPlugin, "_enableSourceSupport"), \
            mock.patch.object(helper.GlobalPlugin, "_enableStoreEnhancements"), \
            mock.patch.object(helper.GlobalPlugin, "_addToolsMenuItems"), \
            mock.patch.object(helper.GlobalPlugin, "_registerSettingsPanel"), \
            mock.patch.object(helper.GlobalPlugin, "_refreshStore"):
            plugin = helper.GlobalPlugin()
        self.assertEqual([helper.MIRROR_STORE_URL], seen)
        self.assertEqual("", plugin._originalURL)
        self.assertEqual("", fixture.config.conf["serrebiStore"]["originalStoreURL"])
        self.assertEqual("", fixture.config.conf["addonStore"]["baseServerURL"])

    def test_failed_policy_install_leaves_store_url_and_ui_untouched(self):
        fixture = helperTests.HelperSourceSupportTests()
        helper = fixture._loadHelper({})
        fixture.config.conf["addonStore"]["baseServerURL"] = "https://old.example"
        fixture.config.conf["serrebiStore"] = {
            "originalStoreURL": "", "originalStoreCaptured": False,
            "searchAsYouType": True,
        }
        globalVars = types.SimpleNamespace(appArgs=types.SimpleNamespace(secure=False))
        calls = []
        with mock.patch.dict(sys.modules, {"globalVars": globalVars}), \
            mock.patch.object(helper.GlobalPlugin, "_enableStorePolicy", return_value=False), \
            mock.patch.object(helper.GlobalPlugin, "_removeStaleBundleModule"), \
            mock.patch.object(helper.GlobalPlugin, "_enableSourceSupport", side_effect=lambda: calls.append("source")), \
            mock.patch.object(helper.GlobalPlugin, "_enableStoreEnhancements", side_effect=lambda: calls.append("enhancements")), \
            mock.patch.object(helper.GlobalPlugin, "_addToolsMenuItems", side_effect=lambda: calls.append("menu")), \
            mock.patch.object(helper.GlobalPlugin, "_registerSettingsPanel", side_effect=lambda: calls.append("settings")), \
            mock.patch.object(helper.GlobalPlugin, "_refreshStore", side_effect=lambda: calls.append("refresh")):
            plugin = helper.GlobalPlugin()
        self.assertFalse(plugin._urlApplied)
        self.assertEqual("https://old.example", fixture.config.conf["addonStore"]["baseServerURL"])
        self.assertEqual([], calls)

    def test_secure_startup_changes_no_policy_or_store_state(self):
        fixture = helperTests.HelperSourceSupportTests()
        helper = fixture._loadHelper({})
        globalVars = types.SimpleNamespace(appArgs=types.SimpleNamespace(secure=True))
        with mock.patch.dict(sys.modules, {"globalVars": globalVars}):
            plugin = helper.GlobalPlugin()
        self.assertFalse(plugin._urlApplied)
        self.assertEqual([], plugin._sourceSupportPatches)
        self.assertEqual("", fixture.config.conf["addonStore"]["baseServerURL"])
        self.assertNotIn("originalStoreCaptured", fixture.config.conf["serrebiStore"])

    def _router(self, directory, defaultURL="https://mirror.example"):
        baseURL = {"value": "https://official.example"}
        seen = []
        class Network:
            _DEFAULT_BASE_URL = "https://official.example"
            @staticmethod
            def _getBaseURL():
                return baseURL["value"] or Network._DEFAULT_BASE_URL
        class Manager:
            def __init__(self):
                self._latestAddonCache = self._compatibleAddonCache = None
                self._cacheLatestFile = str(Path(directory) / "latest.json")
                self._cacheCompatibleFile = str(Path(directory) / "compatible.json")
                self.writeEnabled = True
            def getLatestCompatibleAddons(self):
                seen.append(Network._getBaseURL())
                return seen[-1]
            getLatestAddons = getLatestCompatibleAddons
            def _cacheCompatibleAddons(self, *, addonData, cacheHash):
                if self.writeEnabled and addonData and cacheHash:
                    with open(self._cacheCompatibleFile, "w", encoding="utf-8") as file:
                        json.dump({"data": addonData, "cacheHash": cacheHash}, file)
            def _cacheLatestAddons(self, *, addonData, cacheHash):
                if self.writeEnabled and addonData and cacheHash:
                    with open(self._cacheLatestFile, "w", encoding="utf-8") as file:
                        json.dump({"data": addonData, "cacheHash": cacheHash}, file)
            def _getCachedAddonData(self, path):
                return {"loaded": path}
        class DataManager:
            _DataManager = Manager
        class StoreVM:
            def __init__(self):
                pass
            def _getAvailableAddonsInBG(self):
                return Network._getBaseURL()
        class Store:
            AddonStoreVM = StoreVM
        def patch(owner, name, replacement):
            setattr(owner, name, replacement)
        router = policy.Router(defaultURL)
        router.install(Network, DataManager, Store, patch)
        return router, Network, Manager, Store, baseURL, seen

    def test_empty_temporary_source_uses_core_official_while_default_uses_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            router, network, managerClass, _store, _baseURL, seen = self._router(directory)
            manager = managerClass()
            self.assertEqual("https://mirror.example", manager.getLatestCompatibleAddons())
            with router.source(""):
                self.assertEqual("https://official.example", manager.getLatestCompatibleAddons())
            self.assertEqual(
                ["https://mirror.example", "https://official.example"], seen,
            )

    def test_keyword_cache_write_is_marked_but_skipped_write_is_not_relabelled(self):
        with tempfile.TemporaryDirectory() as directory:
            router, _network, managerClass, _store, _baseURL, _seen = self._router(directory)
            manager = managerClass()
            with router.source("https://source.example"):
                manager._cacheCompatibleAddons(addonData="new", cacheHash="hash")
            with open(manager._cacheCompatibleFile, encoding="utf-8") as file:
                self.assertEqual("https://source.example", json.load(file)["serrebiStoreSource"])
            with open(manager._cacheCompatibleFile, "w", encoding="utf-8") as file:
                json.dump({"serrebiStoreSource": "wrong-source"}, file)
            manager.writeEnabled = False
            with router.source("https://source.example"):
                manager._cacheCompatibleAddons(addonData="new", cacheHash="hash")
            with open(manager._cacheCompatibleFile, encoding="utf-8") as file:
                self.assertEqual("wrong-source", json.load(file)["serrebiStoreSource"])

    def test_concurrent_contexts_are_serialized_and_ignore_later_default_change(self):
        with tempfile.TemporaryDirectory() as directory:
            router, _network, managerClass, _store, _baseURL, seen = self._router(directory)
            manager = managerClass()
            def fetch(url):
                with router.source(url):
                    manager.getLatestCompatibleAddons()
            router.lock.acquire()
            first = threading.Thread(target=fetch, args=("https://one.example",))
            second = threading.Thread(target=fetch, args=("https://two.example",))
            first.start()
            second.start()
            router.defaultURL = "https://changed.example"
            router.lock.release()
            first.join(1); second.join(1)
            self.assertEqual(
                ["https://one.example", "https://two.example"],
                sorted(seen),
            )

    def test_corrupt_cache_root_is_rejected_without_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            router, _network, managerClass, _store, _baseURL, _seen = self._router(directory)
            manager = managerClass()
            for document in ("[]", "null"):
                with open(manager._cacheLatestFile, "w", encoding="utf-8") as file:
                    file.write(document)
                with router.source("https://mirror.example"):
                    self.assertIsNone(manager._getCachedAddonData(manager._cacheLatestFile))

    def test_view_model_keeps_temporary_source_after_default_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            router, _network, _manager, store, _baseURL, _seen = self._router(directory)
            with router.source("https://temporary.example"):
                vm = store.AddonStoreVM()
            router.defaultURL = "https://changed.example"
            self.assertEqual("https://temporary.example", vm._getAvailableAddonsInBG())

    def test_prepare_restore_keeps_active_request_on_its_old_source(self):
        with tempfile.TemporaryDirectory() as directory:
            router, network, _manager, _store, baseURL, _seen = self._router(directory)
            router._begin()
            baseURL["value"] = "https://core-current.example"
            router.prepareRestore()
            self.assertEqual("https://core-current.example", network._getBaseURL())
            with router.source(""):
                self.assertEqual("https://official.example", network._getBaseURL())
            with router.source("https://old-request.example"):
                self.assertEqual("https://old-request.example", network._getBaseURL())
            router._end()
            self.assertEqual("https://core-current.example", network._getBaseURL())

    def test_normal_policy_restore_keeps_records_for_source_restore(self):
        fixture = helperTests.HelperSourceSupportTests()
        helper = fixture._loadHelper({})
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        plugin._policyProfileSwitchRegistered = False
        class StoreVM:
            def __init__(self):
                self.value = "native"
        native = StoreVM.__init__
        def policyInit(vm):
            vm.value = "policy"
        def outerFeatureInit(vm):
            policyInit(vm)
            vm.value = "outer"
        StoreVM.__init__ = outerFeatureInit
        prepared = []
        router = types.SimpleNamespace(
            prepareRestore=lambda: prepared.append(True),
            owned={policyInit},
        )
        plugin._policyRouter = router
        plugin._sourceSupportPatches = [
            (StoreVM, "__init__", native, policyInit),
            (StoreVM, "__init__", policyInit, outerFeatureInit),
        ]
        originalRecords = list(plugin._sourceSupportPatches)
        helper.GlobalPlugin._restoreStorePolicy(plugin)
        self.assertEqual([True], prepared)
        self.assertEqual(originalRecords, plugin._sourceSupportPatches)
        helper.GlobalPlugin._restoreSourceSupport(plugin)
        self.assertIs(native, StoreVM.__init__)

    def test_post_patch_fetch_waits_for_pre_patch_initial_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            finished = []
            class InitialWorker:
                def is_alive(self):
                    return not finished
                def join(self):
                    finished.append(True)
            router, _network, managerClass, _store, _baseURL, _seen = self._router(directory)
            router.initialThread = InitialWorker()
            manager = managerClass()
            manager.getLatestCompatibleAddons()
            self.assertEqual([True], finished)

    def test_initial_worker_keeps_initial_source_for_cache_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            router, _network, managerClass, _store, _baseURL, seen = self._router(directory)
            router.defaultURL = "https://selected.example"
            router.initialURL = "https://before-helper.example"
            router.initialThread = threading.current_thread()
            manager = managerClass()
            self.assertEqual("https://before-helper.example", manager.getLatestCompatibleAddons())
            self.assertEqual(["https://before-helper.example"], seen)
            self.assertIn("https://before-helper.example", router.caches)
            self.assertNotIn("https://selected.example", router.caches)

    def test_profile_switch_callback_accepts_nvda_prev_conf_keyword(self):
        fixture = helperTests.HelperSourceSupportTests()
        helper = fixture._loadHelper({})
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        plugin._policyRouter = types.SimpleNamespace(defaultURL=None)
        fixture.config.conf["serrebiStore"] = {"storePolicy": "official"}
        fixture.config.conf["addonStore"] = {"baseServerURL": "https://old.example"}
        globalPlugins = types.ModuleType("globalPlugins")
        globalPlugins.__path__ = []
        globalVars = types.ModuleType("globalVars")
        globalVars.appArgs = types.SimpleNamespace(secure=False)
        old = {
            "globalPlugins": sys.modules.get("globalPlugins"),
            "globalPlugins._addonStorePolicy": sys.modules.get("globalPlugins._addonStorePolicy"),
            "globalVars": sys.modules.get("globalVars"),
        }
        try:
            sys.modules.update({
                "globalPlugins": globalPlugins,
                "globalPlugins._addonStorePolicy": policy,
                "globalVars": globalVars,
            })
            helper.GlobalPlugin._onPolicyProfileSwitch(plugin, prevConf={})
        finally:
            for name, value in old.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
        self.assertEqual("", fixture.config.conf["addonStore"]["baseServerURL"])
        self.assertEqual("", plugin._policyRouter.defaultURL)


if __name__ == "__main__":
    unittest.main()
