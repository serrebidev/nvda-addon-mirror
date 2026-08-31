import builtins
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "helper"
    / "globalPlugins"
    / "addonStoreMirror.py"
)


def _package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    return module


class _Log:
    def info(self, _message):
        pass

    def exception(self, _message):
        raise AssertionError(_message)


class HelperSourceSupportTests(unittest.TestCase):
    def _loadHelper(self, extraModules):
        config = types.ModuleType("config")

        class Conf(dict):
            spec = {}

        config.conf = Conf(
            addonStore={"baseServerURL": ""},
        )
        addonHandler = types.ModuleType("addonHandler")
        addonHandler.initTranslation = lambda: None
        globalPluginHandler = types.ModuleType("globalPluginHandler")
        globalPluginHandler.GlobalPlugin = object
        logHandler = types.ModuleType("logHandler")
        logHandler.log = _Log()
        modules = {
            "addonHandler": addonHandler,
            "config": config,
            "globalPluginHandler": globalPluginHandler,
            "logHandler": logHandler,
            **extraModules,
        }
        spec = importlib.util.spec_from_file_location(
            "addonStoreMirror_test",
            HELPER_PATH,
        )
        helper = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins,
            "_",
            lambda text: text,
            create=True,
        ):
            spec.loader.exec_module(helper)
        return helper

    def test_current_nvda_models_list_column_search_and_restore(self):
        modelModule = types.ModuleType("addonStore.models.addon")

        class ModelBase:
            def asdict(self):
                return {"addonId": "example"}

        class Model(ModelBase):
            pass

        def createStoreModel(_data):
            return Model()

        def createInstalledStoreModel(_data):
            return Model()

        modelModule._AddonGUIModel = ModelBase
        modelModule._createStoreModelFromData = createStoreModel
        modelModule._createInstalledStoreModelFromData = createInstalledStoreModel

        listControlModule = types.ModuleType("gui.addonStoreGui.controls.addonList")

        class AddonVirtualList:
            def __init__(self, listViewModel):
                self._addonsListVM = listViewModel
                self.columns = []

            def _refreshColumns(self):
                self.columns = ["Name"]

            def GetColumnCount(self):
                return len(self.columns)

            def InsertColumn(self, _index, label, width):
                self.columns.append((label, width))

            def scaleSize(self, size):
                return size

            def OnGetItemText(self, _itemIndex, _colIndex):
                return "Example"

            def OnColClick(self, _event):
                raise AssertionError("Source click reached NVDA's sorter")

        listControlModule.AddonVirtualList = AddonVirtualList

        listViewModelModule = types.ModuleType("gui.addonStoreGui.viewModels.addonList")

        class AddonListItemVM:
            def __init__(self, model):
                self.model = model

            @property
            def searchableText(self):
                return "example addon"

        class AddonListVM:
            presentedFields = ("name",)

            def __init__(self, item):
                self.item = item

            def getAddonAtIndex(self, _index):
                return self.item

        listViewModelModule.AddonListItemVM = AddonListItemVM
        listViewModelModule.AddonListVM = AddonListVM

        modules = {
            "addonStore": _package("addonStore"),
            "addonStore.models": _package("addonStore.models"),
            "addonStore.models.addon": modelModule,
            "gui": _package("gui"),
            "gui.addonStoreGui": _package("gui.addonStoreGui"),
            "gui.addonStoreGui.controls": _package("gui.addonStoreGui.controls"),
            "gui.addonStoreGui.controls.addonList": listControlModule,
            "gui.addonStoreGui.viewModels": _package("gui.addonStoreGui.viewModels"),
            "gui.addonStoreGui.viewModels.addonList": listViewModelModule,
        }
        helper = self._loadHelper(modules)
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        plugin._sourceSupportPatches = []

        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins,
            "_",
            lambda text: text,
            create=True,
        ):
            plugin._enableSourceSupport()

            model = modelModule._createStoreModelFromData(
                {"storeSource": "NV Access Add-on Store"},
            )
            item = listViewModelModule.AddonListItemVM(model)
            listViewModel = AddonListVM(item)
            control = listControlModule.AddonVirtualList(listViewModel)
            control._refreshColumns()

            self.assertEqual(("Source", 140), control.columns[-1])
            self.assertEqual(
                "NV Access Add-on Store",
                control.OnGetItemText(0, 1),
            )
            self.assertIn("nv access add-on store", item.searchableText)
            self.assertEqual(
                "NV Access Add-on Store",
                model.asdict()["storeSource"],
            )

            event = types.SimpleNamespace(GetColumn=lambda: 1)
            self.assertIsNone(control.OnColClick(event))

            plugin._restoreSourceSupport()

        self.assertIs(modelModule._createStoreModelFromData, createStoreModel)
        self.assertIs(
            modelModule._createInstalledStoreModelFromData,
            createInstalledStoreModel,
        )
        self.assertEqual(AddonVirtualList._refreshColumns.__name__, "_refreshColumns")


if __name__ == "__main__":
    unittest.main()
