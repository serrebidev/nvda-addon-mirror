"""Focused checks for the changelog and source-date adapter."""
import importlib.util
from pathlib import Path
import sys
import threading
import types
from http.client import IncompleteRead
import unittest
from urllib.error import HTTPError
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "helper" / "globalPlugins" / "_addonStoreChangelogs.py"
SPEC = importlib.util.spec_from_file_location("addonStoreChangelogs", PATH)
changelogs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changelogs)


class Model:
    def __init__(self, **values):
        self.__dict__.update(values)


class ChangelogTests(unittest.TestCase):
    def setUp(self):
        self.securePatch = mock.patch.object(changelogs, "isSecureDesktop", return_value=False)
        self.securePatch.start()
        self.addCleanup(self.securePatch.stop)

    def test_non_finite_or_boolean_dates_are_unknown(self):
        for value in (True, float("nan"), float("inf"), -1):
            self.assertIsNone(changelogs.releaseTime(Model(_serrebiReleaseTime=value)))
            self.assertIsNone(changelogs.publicationTime(Model(_serrebiOriginalPublicationTime=value)))
            self.assertIsNone(changelogs.updatedTime(Model(submissionTime=value)))

    def test_date_sorting_keeps_unknown_last_and_separates_publication_from_updates(self):
        old = Model(displayName="Old", addonId="old", submissionTime=30,
                    _serrebiOriginalPublicationTime=10)
        new = Model(displayName="New", addonId="new", submissionTime=10,
                    _serrebiOriginalPublicationTime=30)
        unknown = Model(displayName="Unknown", addonId="unknown", submissionTime=None,
                        _serrebiOriginalPublicationTime=None)
        tiedB = Model(displayName="Same", addonId="b", submissionTime=20,
                      _serrebiOriginalPublicationTime=20)
        tiedA = Model(displayName="Same", addonId="a", submissionTime=20,
                      _serrebiOriginalPublicationTime=20)
        models = [unknown, tiedB, old, new, tiedA]
        self.assertEqual(
            [old, tiedA, tiedB, new, unknown],
            sorted(models, key=changelogs.publicationSortKey),
        )
        self.assertEqual(
            [old, tiedA, tiedB, new, unknown],
            sorted(models, key=lambda model: changelogs.sortKey(model, True)),
        )
        self.assertEqual(
            [new, tiedA, tiedB, old, unknown],
            sorted(models, key=lambda model: changelogs.publicationSortKey(model, True)),
        )
        self.assertEqual(
            [new, tiedA, tiedB, old, unknown],
            sorted(models, key=changelogs.sortKey),
        )

    def test_history_cache_has_a_size_bound(self):
        history = changelogs.ReleaseHistory(fetch=lambda _repo: [])
        for index in range(changelogs.MAX_CACHED_REPOSITORIES + 1):
            history.get("owner/repo%d" % index)
        self.assertEqual(changelogs.MAX_CACHED_REPOSITORIES, len(history._cache))

    def test_only_canonical_https_github_repository_is_accepted(self):
        self.assertEqual("owner/project", changelogs.githubRepository("https://github.com/owner/project"))
        for value in (
            "http://github.com/owner/project", "https://github.com/owner/project/issues",
            "https://user:password@github.com/owner/project", "https://evil.example/github.com/owner/project",
        ):
            self.assertIsNone(changelogs.githubRepository(value))

    def test_catalog_notes_precede_github_history_and_fallback_is_explicit(self):
        model = Model(sourceURL="https://github.com/owner/project", addonVersionName="2.0", homepage=None)
        setattr(model, changelogs.MODEL_CHANGELOG_ATTRIBUTE, "Latest correction.")
        history = changelogs.ReleaseHistory(fetch=lambda _repo: [{"tag_name": "1.0", "body": "Older note."}])
        self.assertEqual(
            [("2.0", "Latest correction.", "catalog"), ("1.0", "Older note.", "GitHub release")],
            changelogs.historyForModel(model, history),
        )
        failed = changelogs.ReleaseHistory(
            fetch=lambda _repo: (_ for _ in ()).throw(HTTPError("x", 429, "", {}, None)),
        )
        self.assertEqual("catalog; rateLimit", changelogs.historyForModel(model, failed)[0][2])

    def test_source_preference_gates_fetching_and_does_not_mix_notes(self):
        model = Model(sourceURL="https://github.com/owner/project", addonVersionName="2.0", homepage=None)
        setattr(model, changelogs.MODEL_CHANGELOG_ATTRIBUTE, "Catalog correction.")
        calls = []
        history = changelogs.ReleaseHistory(
            fetch=lambda repository: calls.append(repository) or [{"tag_name": "1.0", "body": "GitHub notes."}],
        )
        catalog = changelogs.historyForModel(model, history, "catalog")
        self.assertEqual([], calls)
        self.assertEqual([("2.0", "Catalog correction.", "catalog")], catalog)
        github = changelogs.historyForModel(model, history, "github")
        self.assertEqual([("1.0", "GitHub notes.", "GitHub release")], github)
        self.assertNotIn("Catalog correction.", github[0][1])

    def test_github_only_failure_does_not_fall_back_to_unrequested_catalog_notes(self):
        model = Model(sourceURL="https://github.com/owner/project", addonVersionName="2.0", homepage=None)
        setattr(model, changelogs.MODEL_CHANGELOG_ATTRIBUTE, "Catalog correction.")
        history = changelogs.ReleaseHistory(fetch=lambda _repo: (_ for _ in ()).throw(OSError("offline")))
        rows = changelogs.historyForModel(model, history, "github")
        self.assertNotIn("Catalog correction.", rows[0][1])
        self.assertEqual("networkError", rows[0][2])

    def test_native_manifest_changelog_is_used_without_store_factory_metadata(self):
        # NVDA 2025.1's external AddonManifestModel exposes the manifest but
        # does not provide the newer ``changelog`` property.
        model = Model(sourceURL=None, addonVersionName="1.0", homepage=None,
                      manifest={"changelog": "Manifest notes."})
        self.assertEqual(
            [("1.0", "Manifest notes.", "catalog; notGitHub")],
            changelogs.historyForModel(model, changelogs.ReleaseHistory(fetch=lambda _repo: [])),
        )

    def test_incomplete_http_read_uses_network_fallback(self):
        model = Model(sourceURL="https://github.com/owner/project", addonVersionName="1.0", homepage=None)
        history = changelogs.ReleaseHistory(
            fetch=lambda _repo: (_ for _ in ()).throw(IncompleteRead(b"partial", 10)),
        )
        self.assertEqual("networkError", changelogs.historyForModel(model, history)[0][2])

    def test_async_requests_share_a_repository_fetch_and_bound_workers(self):
        started = []
        release = threading.Event()
        complete = threading.Event()
        lock = threading.Lock()
        active = [0]
        peak = [0]

        def fetch(repository):
            with lock:
                started.append(repository)
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            release.wait(2)
            with lock:
                active[0] -= 1
            return [{"tag_name": repository, "body": "Notes"}]

        history = changelogs.ReleaseHistory(fetch=fetch)
        results = []
        expected = changelogs.MAX_CONCURRENT_FETCHES + 3

        def received(result, error):
            results.append((result, error))
            if len(results) == expected:
                complete.set()

        # These must join one fetch, while different repositories never run
        # more than the scheduler's worker limit at the same time.
        history.getAsync("owner/shared", received)
        history.getAsync("owner/shared", received)
        for index in range(expected - 2):
            history.getAsync("owner/repo%d" % index, received)
        self.assertEqual(1, started.count("owner/shared"))
        self.assertLessEqual(peak[0], changelogs.MAX_CONCURRENT_FETCHES)
        release.set()
        self.assertTrue(complete.wait(2))
        self.assertEqual(1, started.count("owner/shared"))
        self.assertLessEqual(peak[0], changelogs.MAX_CONCURRENT_FETCHES)
        self.assertEqual(expected, len(results))

    def test_failed_refresh_keeps_successful_cached_notes(self):
        now = [0]
        responses = [[{"tag_name": "1.0", "body": "Good notes"}], OSError("offline")]
        history = changelogs.ReleaseHistory(fetch=lambda _repo: responses.pop(0), now=lambda: now[0])
        self.assertEqual(([{"tag_name": "1.0", "body": "Good notes"}], None), history.get("owner/project"))
        now[0] += changelogs.CACHE_SECONDS + 1
        self.assertEqual(
            ([{"tag_name": "1.0", "body": "Good notes"}], "networkError"), history.get("owner/project"),
        )

    def test_cache_prevents_repeat_fetch_inside_ttl(self):
        calls = []
        now = [100]
        history = changelogs.ReleaseHistory(fetch=lambda repo: calls.append(repo) or [], now=lambda: now[0])
        history.get("owner/project")
        history.get("owner/project")
        self.assertEqual(["owner/project"], calls)

    def test_last_updated_choice_mapping_works_with_minimal_and_current_native_choices(self):
        # NVDA's current list includes Rank, while the minimal fixture does not.
        # The two appended choices must be addressed relative to the native list.
        for nativeChoices in (
            ["Name (ascending)", "Name (descending)"],
            ["Rank (ascending)", "Rank (descending)", "Name (ascending)"],
        ):
            class Item:
                def __init__(self, name, published, updated):
                    self.Id = name
                    self.model = Model(displayName=name, addonId=name, submissionTime=updated,
                                       _serrebiOriginalPublicationTime=published)
            class List:
                _columnSortChoices = property(lambda self: nativeChoices)
                def __init__(self):
                    self._addons = {
                        "updatedUnknown": Item("updatedUnknown", 999, None),
                        "publicationUnknown": Item("publicationUnknown", None, 15),
                        "old": Item("old", 1, 10), "new": Item("new", 2, 20),
                    }
                    self._filterString = None; self._reverseSort = False; self._addonsFilteredOrdered = []
                    self.updated = types.SimpleNamespace(notify=lambda: None)
                def _getFilteredSortedIds(self):
                    return getattr(self, "allowed", ["updatedUnknown", "publicationUnknown", "old", "new"])
                def _getAddonFieldText(self, item, field):
                    return getattr(item.model, field.name, "")
                def _updateAddonListing(self): self._addonsFilteredOrdered = self._getFilteredSortedIds()
                def setSortField(self, *_args, **_kwargs): self.nativeCalled = True
            listModule = types.ModuleType("gui.addonStoreGui.viewModels.addonList")
            listModule.AddonListVM = List
            publicationField = types.SimpleNamespace(name="publicationDate", displayString="Publication date")
            listModule.AddonListField = types.SimpleNamespace(publicationDate=publicationField)
            class StoreContext:
                def __init__(self, vm): self.listVM = vm
            class Dialog:
                def __init__(self, vm): self._storeVM = StoreContext(vm)
                def Bind(self, _event, handler): self.destroyHandler = handler
                def onColumnFilterChange(self, _event):
                    self.nativeCalled = True
                    self._storeVM.listVM.setSortField("native")
            dialogModule = types.ModuleType("gui.addonStoreGui.controls.storeDialog")
            dialogModule.AddonStoreDialog = Dialog
            wx = types.ModuleType("wx")
            wx.EVT_WINDOW_DESTROY = object()
            modelModule = types.ModuleType("addonStore.models.addon")
            modelModule._AddonGUIModel = type("Base", (), {"asdict": lambda self: {}})
            modelModule._AddonStoreModel = type("StoreModel", (), {"publicationDate": property(lambda self: None)})
            modelModule._createStoreModelFromData = lambda _data: Model()
            modelModule._createInstalledStoreModelFromData = lambda _data: Model()
            storeModule = types.ModuleType("gui.addonStoreGui.viewModels.store")
            storeModule.AddonStoreVM = type("Store", (), {"_makeActionsList": lambda self: []})
            modules = {
                "addonStore.models.addon": modelModule, "gui.addonStoreGui.viewModels.addonList": listModule,
                "gui.addonStoreGui.controls.storeDialog": dialogModule,
                "gui.addonStoreGui.viewModels.store": storeModule, "wx": wx,
            }
            class Plugin:
                def _rememberPatch(self, owner, name, replacement): setattr(owner, name, replacement)
            old = {name: sys.modules.get(name) for name in modules}
            try:
                sys.modules.update(modules)
                feature = changelogs.ChangelogFeature(Plugin())
                feature.enable()
                vm = List()
                dialog = Dialog(vm)
                self.assertIs(feature._dialogs[dialog._storeVM](), dialog)
                firstAdded = len(nativeChoices)
                dialog.onColumnFilterChange(types.SimpleNamespace(GetSelection=lambda: firstAdded))
                self.assertFalse(vm._serrebiDateSort)
                self.assertEqual(["old", "publicationUnknown", "new", "updatedUnknown"], vm._addonsFilteredOrdered)
                dialog.onColumnFilterChange(types.SimpleNamespace(GetSelection=lambda: firstAdded + 1))
                self.assertTrue(vm._serrebiDateSort)
                self.assertEqual(["new", "publicationUnknown", "old", "updatedUnknown"], vm._addonsFilteredOrdered)
                # A source filter can narrow results without reintroducing native order.
                vm.allowed = ["old", "updatedUnknown"]
                dialog.onColumnFilterChange(types.SimpleNamespace(GetSelection=lambda: firstAdded))
                self.assertEqual(["old", "updatedUnknown"], vm._addonsFilteredOrdered)
                dialog.onColumnFilterChange(types.SimpleNamespace(GetSelection=lambda: 0))
                self.assertIsNone(vm._serrebiDateSort)
                self.assertTrue(dialog.nativeCalled)
                vm._sortByModelField = types.SimpleNamespace(name="publicationDate")
                vm._reverseSort = False
                vm.allowed = ["updatedUnknown", "publicationUnknown", "old", "new"]
                vm._updateAddonListing()
                self.assertEqual(["old", "publicationUnknown", "new", "updatedUnknown"], vm._addonsFilteredOrdered)
                vm._reverseSort = True
                vm._updateAddonListing()
                self.assertEqual(["new", "publicationUnknown", "old", "updatedUnknown"], vm._addonsFilteredOrdered)
                event = types.SimpleNamespace(GetEventObject=lambda: dialog, Skip=lambda: None)
                dialog.destroyHandler(event)
                self.assertFalse(dialog._serrebiChangelogAlive)
                self.assertNotIn(dialog._storeVM, feature._dialogs)
            finally:
                for name, value in old.items():
                    if value is None: sys.modules.pop(name, None)
                    else: sys.modules[name] = value

    def test_factory_and_cache_round_trip_keep_extension_metadata(self):
        class Base:
            def asdict(self):
                return {"addonId": "example"}
        class Model(Base):
            pass
        modelModule = types.ModuleType("addonStore.models.addon")
        modelModule._AddonGUIModel = Base
        modelModule._AddonStoreModel = type("StoreModel", (), {"publicationDate": property(lambda self: None)})
        modelModule._createStoreModelFromData = lambda _data: Model()
        modelModule._createInstalledStoreModelFromData = lambda _data: Model()
        dataManager = types.ModuleType("addonStore.dataManager")
        dataManager._createStoreModelFromData = modelModule._createStoreModelFromData
        dataManager._createInstalledStoreModelFromData = modelModule._createInstalledStoreModelFromData
        listModule = types.ModuleType("gui.addonStoreGui.viewModels.addonList")
        publicationField = types.SimpleNamespace(name="publicationDate", displayString="Publication date")
        listModule.AddonListField = types.SimpleNamespace(publicationDate=publicationField)
        listModule.AddonListVM = type(
            "List",
            (),
            {
                "_getFilteredSortedIds": lambda self: [],
                "setSortField": lambda self, *_args, **_kwargs: None,
                "_getAddonFieldText": lambda self, item, field: "",
                "_columnSortChoices": property(lambda self: ["Name (ascending)"]),
            },
        )
        storeModule = types.ModuleType("gui.addonStoreGui.viewModels.store")
        storeModule.AddonStoreVM = type("Store", (), {"_makeActionsList": lambda self: []})
        dialogModule = types.ModuleType("gui.addonStoreGui.controls.storeDialog")
        dialogModule.AddonStoreDialog = type(
            "Dialog",
            (),
            {"__init__": lambda self, *_args, **_kwargs: None, "onColumnFilterChange": lambda self, evt: None},
        )
        wx = types.ModuleType("wx")
        wx.EVT_WINDOW_DESTROY = object()
        modules = {
            "addonStore.models.addon": modelModule, "addonStore.dataManager": dataManager,
            "gui.addonStoreGui.viewModels.addonList": listModule,
            "gui.addonStoreGui.viewModels.store": storeModule,
            "gui.addonStoreGui.controls.storeDialog": dialogModule,
            "wx": wx,
        }
        class Plugin:
            def __init__(self): self.patches = []
            def _rememberPatch(self, owner, name, replacement):
                original = getattr(owner, name)
                setattr(owner, name, replacement)
                self.patches.append((owner, name, original, replacement))
        plugin = Plugin()
        old = dict((name, sys.modules.get(name)) for name in modules)
        try:
            sys.modules.update(modules)
            changelogs.ChangelogFeature(plugin).enable()
            model = modelModule._createStoreModelFromData(
                {
                    "changelog": "Notes", "releaseTime": 123, "lastUpdatedTime": 789,
                    "originalPublicationTime": 111,
                    "originalPublicationSource": "https://evidence.example/initial",
                },
            )
            self.assertEqual("Notes", model.asdict()["changelog"])
            self.assertEqual(123, model.asdict()["releaseTime"])
            self.assertEqual(789, model.asdict()["lastUpdatedTime"])
            self.assertEqual(111, model.asdict()["originalPublicationTime"])
            self.assertEqual(
                "https://evidence.example/initial", model.asdict()["originalPublicationSource"],
            )
            cached = dataManager._createInstalledStoreModelFromData(
                {"changelog": "Cached", "releaseTime": 456, "lastUpdatedTime": 987},
            )
            self.assertEqual("Cached", cached.asdict()["changelog"])
            self.assertEqual(987, cached.asdict()["lastUpdatedTime"])
        finally:
            for name, value in old.items():
                if value is None: sys.modules.pop(name, None)
                else: sys.modules[name] = value
