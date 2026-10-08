import importlib.util
import pathlib
import tempfile
import unittest
import json
from types import SimpleNamespace


_PATH = pathlib.Path(__file__).parents[1] / "helper" / "globalPlugins" / "_addonStorePolicy.py"
_SPEC = importlib.util.spec_from_file_location("policy", _PATH)
policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(policy)


class StorePolicyTests(unittest.TestCase):
    def test_selected_url_validates_policy_and_custom_url(self):
        self.assertEqual(policy.MIRROR, policy.selectedURL({}))
        self.assertEqual("", policy.selectedURL({"storePolicy": "official"}))
        self.assertEqual("https://example.org", policy.selectedURL({
            "storePolicy": "custom", "customStoreURL": "https://example.org/",
        }))
        self.assertEqual(policy.MIRROR, policy.selectedURL({
            "storePolicy": "custom", "customStoreURL": "http://example.org",
        }))

    def test_router_captures_source_and_separates_memory_caches(self):
        router = policy.Router("https://default.example")
        manager = SimpleNamespace(
            _latestAddonCache="old", _compatibleAddonCache="old",
            _cacheLatestFile="", _cacheCompatibleFile="", _getCachedAddonData=lambda _path: None,
        )
        with router.source("https://first.example"):
            router._activate(manager, router.currentURL())
            self.assertIsNone(manager._latestAddonCache)
            manager._latestAddonCache = "first"
            router._remember(manager, router.currentURL())
        with router.source("https://second.example"):
            router._activate(manager, router.currentURL())
            self.assertIsNone(manager._latestAddonCache)
            manager._latestAddonCache = "second"
            router._remember(manager, router.currentURL())
        with router.source("https://first.example"):
            router._activate(manager, router.currentURL())
            self.assertEqual("first", manager._latestAddonCache)

    def test_custom_url_rejects_credentials_controls_and_http(self):
        self.assertIsNone(policy.validCustomURL("https://name@example.org/store"))
        self.assertIsNone(policy.validCustomURL("https://example.org/\nstore"))
        self.assertIsNone(policy.validCustomURL("http://example.org"))
        self.assertIsNone(policy.validCustomURL("https://example.org/?query"))
        self.assertIsNone(policy.validCustomURL("https://example.org/#fragment"))
        self.assertIsNone(policy.validCustomURL("https://example.org:70000"))
        self.assertIsNone(policy.validCustomURL("https://[broken"))
        self.assertIsNone(policy.validCustomURL("https://invalid host.example"))

    def test_router_captures_fetch_source_and_official_uses_original_base(self):
        seen = []
        class Network:
            _DEFAULT_BASE_URL = "https://official.example"
            @staticmethod
            def _getBaseURL():
                return "https://official.example"
        class Manager:
            def __init__(self):
                self._latestAddonCache = self._compatibleAddonCache = None
                self._cacheLatestFile = self._cacheCompatibleFile = "missing"
            def getLatestCompatibleAddons(self):
                seen.append(Network._getBaseURL())
                return seen[-1]
            getLatestAddons = getLatestCompatibleAddons
            def _cacheCompatibleAddons(self, *_args): pass
            def _cacheLatestAddons(self, *_args): pass
            def _getCachedAddonData(self, _path): return None
        class DataManager: _DataManager = Manager
        class StoreVM:
            def __init__(self): pass
            def _getAvailableAddonsInBG(self):
                return Network._getBaseURL()
        class Store: AddonStoreVM = StoreVM
        patches = []
        def patch(owner, name, replacement):
            patches.append((owner, name, getattr(owner, name), replacement))
            setattr(owner, name, replacement)
        router = policy.Router("")
        router.install(Network, DataManager, Store, patch)
        self.assertEqual("https://official.example", Network._getBaseURL())
        manager = Manager()
        with router.source("https://temporary.example"):
            self.assertEqual("https://temporary.example", manager.getLatestCompatibleAddons())
        self.assertEqual(["https://temporary.example"], seen)


if __name__ == "__main__":
    unittest.main()
