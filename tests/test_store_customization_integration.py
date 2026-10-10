"""Integration checks for the runtime Add-on Store customization adapters.

The real NVDA modules are Windows/wxPython-only.  These small host doubles keep
the same private contracts used by ``CustomizationFeature.enable`` so the tests
exercise the installed monkeypatches rather than reimplementing their logic.
"""
from collections import OrderedDict
from enum import Enum
import importlib.util
import gc
from pathlib import Path
import sys
import types
import weakref
import unittest
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "helper" / "globalPlugins" / "_addonStoreCustomization.py"


class _Conf(dict):
    def __init__(self):
        super().__init__({"serrebiStore": {}})
        self.spec = {"serrebiStore": {}}


class _Control:
    def __init__(self, choices=None, label=""):
        self.choices = list(choices or [])
        self.label = label
        self.selection = 0
        self.value = False
        self.focused = False

    def Set(self, choices):
        self.choices = list(choices)

    def Append(self, value):
        self.choices.append(value)

    def SetSelection(self, selection):
        self.selection = selection

    def GetSelection(self):
        return self.selection

    def GetCount(self):
        return len(self.choices)

    def SetValue(self, value):
        self.value = value

    def GetValue(self):
        return self.value

    def Show(self):
        pass

    def Enable(self):
        pass

    def Bind(self, event, handler, *args):
        self.handler = handler

    def SetFocus(self):
        self.focused = True

    def _refreshColumns(self):
        self.refreshed = getattr(self, "refreshed", 0) + 1


class _Notebook:
    def __init__(self, page, labels, selection=0):
        self.pages = [page for _label in labels]
        self.labels = list(labels)
        self.selection = selection
        self.bound = None
        self.frozen = False

    def GetPageCount(self):
        return len(self.pages)

    def GetSelection(self):
        return self.selection

    def GetPage(self, index):
        return self.pages[index]

    def Unbind(self, event):
        self.bound = None

    def Bind(self, event, handler, source=None):
        self.bound = handler

    def Freeze(self):
        self.frozen = True

    def Thaw(self):
        self.frozen = False

    def RemovePage(self, index):
        self.pages.pop(index)
        self.labels.pop(index)
        return True

    def AddPage(self, page, label):
        self.pages.append(page)
        self.labels.append(label)

    def ChangeSelection(self, index):
        old = self.selection
        self.selection = index
        return old


class _KeyEvent:
    def __init__(self, key, control=False, alt=False, shift=False):
        self.key = key
        self.control = control
        self.alt = alt
        self.shift = shift
        self.skipped = False

    def GetKeyCode(self):
        return self.key

    def ControlDown(self):
        return self.control

    def AltDown(self):
        return self.alt

    def ShiftDown(self):
        return self.shift

    def Skip(self):
        self.skipped = True


class _StatusKey(Enum):
    INSTALLED = 1
    UPDATE = 2
    AVAILABLE = 3
    INCOMPATIBLE = 4

    @property
    def displayString(self):
        return self.name.title()


class _Channel(Enum):
    ALL = "all"
    STABLE = "stable"
    EXTERNAL = "external"

    @property
    def displayString(self):
        return self.name.title()


class _Field:
    def __init__(self, name, hidden=()):
        self.name = name
        self.displayString = name.title()
        self.hideStatuses = frozenset(hidden)


class _ListItem:
    def __init__(self, model, status="available"):
        self.model = model
        self.status = status

    def canUseRemoveAction(self):
        return bool(getattr(self.model, "removable", False))


class _Action:
    def __init__(self, displayName, actionHandler, validCheck, actionTarget):
        self.displayName = displayName
        self.actionHandler = actionHandler
        self.validCheck = validCheck
        self.actionTarget = actionTarget

    @property
    def isValid(self):
        return self.validCheck(self.actionTarget)


class _ListVM:
    sortableFields = [
        _Field("displayName"),
        _Field("availableAddonVersionName", {_StatusKey.INSTALLED}),
        _Field("author", {_StatusKey.AVAILABLE, _StatusKey.UPDATE}),
        _Field("searchRank", set(_StatusKey)),
    ]
    presentedFields = sortableFields[:3]

    def __init__(self, selected=None):
        self.selected = selected
        self._sortByModelField = self.sortableFields[0]
        self._reverseSort = False
        self._columnSortChoices = [
            f"{field.name} {direction}"
            for field in self.sortableFields
            for direction in ("ascending", "descending")
        ] + ["Last updated ascending", "Last updated descending"]
        self.reset = None

    def getSelection(self):
        return self.selected

    def setSortField(self, field, reverse):
        self._sortByModelField = field
        self._reverseSort = bool(reverse)
        self._serrebiDateSort = None

    def resetListItems(self, rows):
        self.reset = rows


class _Store:
    def __init__(self, nativeRows=None, selected=None):
        self.nativeRows = list(nativeRows or [])
        self.listVM = _ListVM(selected)
        self.detailsVM = types.SimpleNamespace(listItem=None)
        self._installedAddons = {_Channel.STABLE: {}, _Channel.EXTERNAL: {}}
        self._filterChannelKey = _Channel.ALL
        self._filteredStatusKey = _StatusKey.INSTALLED
        self._serrebiFavourites = False
        self.helped = []
        self.removed = []
        self.actionVMList = []

    def _createListItemVMs(self):
        return self.nativeRows

    def _makeActionsList(self):
        return []

    def _filterByEnabledKey(self, model):
        return True

    def helpAddon(self, item):
        self.helped.append(item)

    def removeAddon(self, item):
        self.removed.append(item)

    def removeAddons(self, items):
        self.removed.extend(items)


class _Dialog:
    title = "Add-on Store"

    def __init__(self, *args, **kwargs):
        pass

    def Bind(self, event, handler, *args):
        setattr(self, "_bound_%s" % id(event), handler)

    @property
    def _statusFilterKey(self):
        return self._storeVM._filteredStatusKey

    @property
    def _titleText(self):
        return "native title"

    @property
    def _listLabelText(self):
        return "native label"

    def _createFilterControls(self, *args, **kwargs):
        pass

    def onListTabPageChange(self, event):
        self.tabChanges = getattr(self, "tabChanges", 0) + 1
        self._storeVM._filteredStatusKey = self._statusFilterKey

    def _toggleFilterControls(self):
        pass

    def onColumnFilterChange(self, event):
        self.nativeSortSelections.append(event.GetSelection())

    def onFilterTextChange(self, event):
        pass

    def _setListLabels(self):
        pass

    @property
    def _channelFilterKey(self):
        return self._storeVM._filterChannelKey


class _VirtualList:
    def OnColClick(self, event):
        pass


class _MonoMenu:
    def _appendUpdateChannelSubMenu(self):
        self.usedNativeChannelMenu = True


class _BatchMenu:
    @property
    def _actions(self):
        return []


class _SettingsPanel:
    def makeSettings(self, sizer):
        pass

    def onSave(self):
        pass


class _Plugin:
    def __init__(self):
        self.patches = []
        self.terminated = False

    def _rememberPatch(self, owner, name, replacement):
        self.patches.append((owner, name, getattr(owner, name), replacement))
        setattr(owner, name, replacement)

    def terminate(self):
        self.terminated = True


class StoreCustomizationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config = types.ModuleType("config")
        self.config.conf = _Conf()
        self.focus = [None]
        self.wx = types.ModuleType("wx")
        self.wx.Dialog = object
        self.wx.EVT_NOTEBOOK_PAGE_CHANGED = object()
        self.wx.EVT_CHAR_HOOK = object()
        self.wx.EVT_BUTTON = object()
        self.wx.EVT_LISTBOX = object()
        self.wx.EVT_WINDOW_DESTROY = object()
        self.wx.WXK_RETURN = 13
        self.wx.WXK_NUMPAD_ENTER = 370
        self.wx.ID_OK = 1
        self.wx.ID_CANCEL = 0
        self.wx.CheckBox = lambda *args, **kwargs: _Control(label=kwargs.get("label", ""))
        self.wx.Button = lambda *args, **kwargs: _Control(label=kwargs.get("label", ""))
        self.wx.Choice = _Control
        self.wx.ListBox = _Control
        self.wx.Window = types.SimpleNamespace(FindFocus=lambda: self.focus[0])

        status = types.ModuleType("addonStore.models.status")
        status._StatusFilterKey = _StatusKey
        status._statusFilters = OrderedDict((key, set()) for key in _StatusKey)
        status.getStatus = lambda model, context: (model.addonId, context.name)
        channels = types.ModuleType("addonStore.models.channel")
        channels.Channel = _Channel
        channels._channelFilters = OrderedDict({
            _Channel.ALL: {_Channel.STABLE, _Channel.EXTERNAL},
            _Channel.STABLE: {_Channel.STABLE},
            _Channel.EXTERNAL: {_Channel.EXTERNAL},
        })
        lists = types.ModuleType("gui.addonStoreGui.viewModels.addonList")
        lists.AddonListItemVM = _ListItem
        lists.AddonListVM = _ListVM
        lists.AddonListField = _ListVM.sortableFields
        stores = types.ModuleType("gui.addonStoreGui.viewModels.store")
        stores.AddonStoreVM = _Store
        dialogs = types.ModuleType("gui.addonStoreGui.controls.storeDialog")
        dialogs.AddonStoreDialog = _Dialog
        controls = types.ModuleType("gui.addonStoreGui.controls.addonList")
        controls.AddonVirtualList = _VirtualList
        actionModule = types.ModuleType("gui.addonStoreGui.viewModels.action")
        actionModule.AddonActionVM = _Action
        actionModule.BatchAddonActionVM = _Action
        menus = types.ModuleType("gui.addonStoreGui.controls.actions")
        menus._MonoActionsContextMenu = _MonoMenu
        menus._BatchActionsContextMenu = _BatchMenu
        menus._UpdateChannelSubMenu = lambda store: types.SimpleNamespace(_contextMenu=object())

        globalVars = types.ModuleType("globalVars")
        globalVars.appArgs = types.SimpleNamespace(secure=False)
        ui = types.ModuleType("ui")
        ui.messages = []
        ui.message = ui.messages.append
        guiHelper = types.SimpleNamespace(BoxSizerHelper=_SizerHelper)
        gui = types.ModuleType("gui")
        gui.guiHelper = guiHelper

        self.hostModules = {
            "config": self.config,
            "wx": self.wx,
            "globalVars": globalVars,
            "ui": ui,
            "gui": gui,
            "addonStore.models.status": status,
            "addonStore.models.channel": channels,
            "gui.addonStoreGui.viewModels.addonList": lists,
            "gui.addonStoreGui.viewModels.store": stores,
            "gui.addonStoreGui.controls.storeDialog": dialogs,
            "gui.addonStoreGui.controls.addonList": controls,
            "gui.addonStoreGui.viewModels.action": actionModule,
            "gui.addonStoreGui.controls.actions": menus,
        }
        self.modulesPatch = mock.patch.dict(sys.modules, self.hostModules)
        self.modulesPatch.start()
        spec = importlib.util.spec_from_file_location("storeCustomizationIntegrationTest", PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.plugin = _Plugin()
        self.feature = self.module.CustomizationFeature(self.plugin)
        self.feature.enable(_SettingsPanel)

    def tearDown(self):
        for owner, name, original, replacement in reversed(self.plugin.patches):
            if getattr(owner, name) is replacement:
                setattr(owner, name, original)
        self.modulesPatch.stop()

    def _dialog(self, nativeSelection=0):
        dialog = object.__new__(_Dialog)
        dialog._storeVM = _Store()
        dialog.addonListTabs = _Notebook(object(), [key.displayString for key in _StatusKey], nativeSelection)
        dialog.channelFilterCtrl = _Control([channel.displayString for channel in _Channel])
        dialog.columnFilterCtrl = _Control()
        dialog.addonListView = _Control()
        dialog.nativeSortSelections = []
        dialog.tabChanges = 0
        return dialog

    def test_semantic_tabs_reorder_without_changing_native_status_context(self):
        self.config.conf["serrebiStore"]["tabOrder"] = [
            "FAVOURITES", "UPDATE", "AVAILABLE", "INSTALLED", "INCOMPATIBLE",
        ]
        dialog = self._dialog(nativeSelection=0)
        self.feature.rebuildTabs(dialog)
        self.assertEqual(
            ["FAVOURITES", "UPDATE", "AVAILABLE", "INSTALLED", "INCOMPATIBLE"],
            dialog._serrebiTabNames,
        )
        self.assertEqual("Installed", dialog.addonListTabs.labels[3])
        self.assertEqual("Favourites", dialog.addonListTabs.labels[0])
        self.assertEqual(3, dialog.addonListTabs.GetSelection())
        dialog.addonListTabs.ChangeSelection(0)
        self.assertIs(_StatusKey.AVAILABLE, dialog._statusFilterKey)
        dialog.addonListTabs.ChangeSelection(1)
        self.assertIs(_StatusKey.UPDATE, dialog._statusFilterKey)

    def test_reordered_sort_is_staged_until_enter_and_ctrl_digit_uses_logical_index(self):
        self.config.conf["serrebiStore"].update({
            "sortOnEnter": True,
            "sortOrder": ["lastUpdated:desc", "displayName:asc"],
            "tabOrder": ["FAVOURITES", "AVAILABLE", "INSTALLED", "UPDATE", "INCOMPATIBLE"],
        })
        dialog = self._dialog()
        self.feature.rebuildTabs(dialog)
        self.feature.syncSort(dialog)
        event = types.SimpleNamespace(GetSelection=lambda: 0)
        dialog.onColumnFilterChange(event)
        self.assertEqual("lastUpdated:desc", dialog._serrebiPendingSort)
        self.assertEqual([], dialog.nativeSortSelections)

        self.focus[0] = dialog.columnFilterCtrl
        self.feature.keyPressed(dialog, _KeyEvent(self.wx.WXK_RETURN))
        self.assertIsNone(dialog._serrebiPendingSort)
        self.assertEqual([len(_ListVM.sortableFields) * 2 + 1], dialog.nativeSortSelections)

        self.feature.keyPressed(dialog, _KeyEvent(ord("2"), control=True))
        self.assertEqual(1, dialog.addonListTabs.GetSelection())
        self.assertEqual(1, dialog.tabChanges)
        self.assertTrue(dialog.addonListView.focused)
        self.assertIs(_StatusKey.AVAILABLE, dialog._storeVM._filteredStatusKey)

    def test_favourites_keep_native_catalogue_rows_and_add_only_missing_installed_rows(self):
        self.config.conf["serrebiStore"]["favourites"] = ["Alpha", "missing"]
        catalog = _ListItem(_model("alpha", _Channel.STABLE))
        ignored = _ListItem(_model("other", _Channel.STABLE))
        store = _Store([catalog, ignored])
        store._serrebiFavourites = True
        installedDuplicate = _model("ALPHA", _Channel.STABLE, removable=True)
        installedMissing = _model("missing", _Channel.EXTERNAL, removable=True)
        store._installedAddons[_Channel.STABLE]["alpha"] = installedDuplicate
        store._installedAddons[_Channel.EXTERNAL]["missing"] = installedMissing

        rows = store._createListItemVMs()
        self.assertEqual(["alpha", "missing"], [row.model.addonId.casefold() for row in rows])
        self.assertIs(catalog, rows[0])
        self.assertIs(installedMissing, rows[1].model)
        self.assertEqual(("missing", "AVAILABLE"), rows[1].status)

    def test_favourite_installed_actions_validate_and_delegate_the_installed_native_item(self):
        self.config.conf["serrebiStore"]["favourites"] = ["alpha"]
        selected = _ListItem(_model("alpha", _Channel.STABLE))
        store = _Store(selected=selected)
        store._serrebiFavourites = True
        installed = _model("ALPHA", _Channel.EXTERNAL, removable=True, doc="help.html")
        store._installedAddons[_Channel.EXTERNAL]["alpha"] = installed

        actions = store._makeActionsList()
        helpAction = next(action for action in actions if action.displayName == "Help for installed &version")
        removeAction = next(action for action in actions if action.displayName == "Remove &installed version")
        self.assertTrue(helpAction.isValid)
        self.assertTrue(removeAction.isValid)
        helpAction.actionHandler(selected)
        removeAction.actionHandler(selected)
        self.assertIs(installed, store.helped[0].model)
        self.assertIs(installed, store.removed[0].model)
        self.assertEqual(("ALPHA", "INSTALLED"), store.removed[0].status)
        store._serrebiFavourites = False
        self.assertFalse(helpAction.isValid)
        self.assertFalse(removeAction.isValid)

    def test_settings_edits_remain_a_draft_until_panel_save(self):
        self.config.conf["serrebiStore"].update({
            "tabOrder": ["INSTALLED", "AVAILABLE"],
            "sortOrder": ["displayName:asc"],
            "tabDefaults": "{}",
        })
        panel = object.__new__(_SettingsPanel)
        panel.makeSettings(object())
        panel._serrebiCustomizationDraft["tabOrder"] = ["FAVOURITES", "AVAILABLE"]
        panel._serrebiCustomizationDraft["tabDefaults"] = {"FAVOURITES": {"channel": "ALL"}}
        self.assertEqual(["INSTALLED", "AVAILABLE"], self.config.conf["serrebiStore"]["tabOrder"])
        self.assertEqual("{}", self.config.conf["serrebiStore"]["tabDefaults"])
        panel.onSave()
        self.assertEqual(["FAVOURITES", "AVAILABLE"], self.config.conf["serrebiStore"]["tabOrder"])
        self.assertEqual(
            {"FAVOURITES": {"channel": "ALL"}},
            __import__("json").loads(self.config.conf["serrebiStore"]["tabDefaults"]),
        )

    def test_save_succeeds_after_native_store_wrapper_deleted(self):
        panel = object.__new__(_SettingsPanel)
        panel.makeSettings(object())
        dialog = self._dialog()
        def deleted():
            raise RuntimeError("wrapped C/C++ object of type AddonStoreDialog has been deleted")
        dialog.IsBeingDeleted = deleted
        self.feature.dialogs.add(dialog)
        panel._serrebiCustomizationChecks["sortOnEnter"].SetValue(True)
        panel.onSave()
        self.assertTrue(self.config.conf["serrebiStore"]["sortOnEnter"])
        self.assertNotIn(dialog, self.feature.dialogs)

    def test_construction_page_event_skips_native_until_controls_ready(self):
        dialog = self._dialog()
        dialog._serrebiBuildingControls = True
        dialog.onListTabPageChange(None)
        self.assertEqual(0, dialog.tabChanges)
        dialog._serrebiBuildingControls = False
        self.feature.rebuildTabs(dialog)
        dialog.onListTabPageChange(None)
        self.assertEqual(1, dialog.tabChanges)

    def test_retained_notebook_callback_does_not_prevent_store_collection(self):
        dialog = self._dialog()
        self.feature.rebuildTabs(dialog)
        callback = dialog.addonListTabs.bound
        reference = weakref.ref(dialog)
        del dialog
        gc.collect()
        self.assertIsNone(reference())
        event = types.SimpleNamespace(Skip=mock.Mock())
        callback(event)
        event.Skip.assert_called_once()

    def test_retained_preferences_button_does_not_prevent_panel_collection(self):
        captured = []
        def record(_helper, item):
            captured.append(item)
            return item
        panel = object.__new__(_SettingsPanel)
        with mock.patch.object(_SizerHelper, "addItem", record):
            panel.makeSettings(object())
        callback = captured[-1].handler
        reference = weakref.ref(panel)
        del panel
        gc.collect()
        self.assertIsNone(reference())
        event = types.SimpleNamespace(Skip=mock.Mock())
        callback(event)
        event.Skip.assert_called_once()

    def test_ctrl_c_shares_only_from_list_and_invalid_action_falls_through(self):
        dialog = self._dialog()
        item = _ListItem(_model("alpha", _Channel.STABLE))
        dialog._storeVM.listVM.selected = item
        handler = mock.Mock()
        action = types.SimpleNamespace(_serrebiShareAction=True, isValid=True,
                                       actionTarget=None, actionHandler=handler)
        dialog._storeVM.actionVMList = [action]
        self.focus[0] = dialog.addonListView
        event = _KeyEvent(ord("C"), control=True)
        self.feature.keyPressed(dialog, event)
        handler.assert_called_once_with(item)
        self.assertFalse(event.skipped)
        self.assertIs(item, action.actionTarget)
        for focus in (dialog.columnFilterCtrl, object()):
            self.focus[0] = focus
            event = _KeyEvent(ord("C"), control=True)
            self.feature.keyPressed(dialog, event)
            self.assertTrue(event.skipped)
        self.focus[0] = dialog.addonListView
        action.isValid = False
        event = _KeyEvent(ord("C"), control=True)
        self.feature.keyPressed(dialog, event)
        self.assertTrue(event.skipped)
        self.assertEqual(1, handler.call_count)

    def test_child_destroy_does_not_deregister_live_store(self):
        dialog = self._dialog()
        _Dialog.__init__(dialog)
        cleanup = getattr(dialog, "_bound_%s" % id(self.wx.EVT_WINDOW_DESTROY))
        child = types.SimpleNamespace(GetEventObject=lambda: dialog.addonListView, Skip=mock.Mock())
        cleanup(child)
        self.assertIn(dialog, self.feature.dialogs)
        event = types.SimpleNamespace(GetEventObject=lambda: dialog, Skip=mock.Mock())
        cleanup(event)
        self.assertNotIn(dialog, self.feature.dialogs)
        event.Skip.assert_called_once()


class _SizerHelper:
    def __init__(self, parent, sizer=None):
        self.parent = parent

    def addItem(self, item):
        return item


def _model(addonId, channel, removable=False, doc=None):
    handler = None if doc is None else types.SimpleNamespace(getDocFilePath=lambda: doc)
    return types.SimpleNamespace(
        addonId=addonId,
        channel=channel,
        legacy=False,
        removable=removable,
        _addonHandlerModel=handler,
    )


if __name__ == "__main__":
    unittest.main()
