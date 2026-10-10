import builtins
from enum import Enum
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
import weakref
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "helper/globalPlugins/_addonStoreBrowsing.py"


class BrowsingTests(unittest.TestCase):
    def setUp(self):
        wx = types.ModuleType("wx")
        wx.Dialog = object
        wx.CallAfter = lambda callback, *args: callback(*args)
        wx.EVT_BUTTON = object()
        class Button:
            def __init__(self, parent, label):
                self.handler = None

            def Bind(self, event, handler):
                self.handler = handler
        wx.Button = Button
        handler = types.ModuleType("addonHandler")
        handler.initTranslation = lambda: None
        config = types.ModuleType("config")
        config.conf = {"serrebiStore": {}}
        self.config = config
        spec = importlib.util.spec_from_file_location("browsing_test", PATH)
        self.module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"wx": wx, "addonHandler": handler, "config": config}), \
                mock.patch.object(builtins, "_", lambda text: text, create=True):
            spec.loader.exec_module(self.module)

    def test_column_order_hidden_fields_and_name_requirement(self):
        fields = [types.SimpleNamespace(name=name) for name in ("displayName", "status", "publisher")]
        layout = self.module._layoutFields(fields, ["source", "status", "source", "obsolete"],
                                            ["publisher", "displayName"])
        self.assertEqual(["source", "status", "displayName"],
                         [field if isinstance(field, str) else field.name for field in layout])
        self.assertEqual(["displayName", "status", "publisher"], [field.name for field in fields])

    def test_invalid_and_future_state_are_ignored(self):
        for text in ("invalid", "[]", '{"version":2,"stores":{"x":{}}}', '{"stores":[]}'):
            self.assertEqual({}, self.module._decodeState(text))
        data = self.module._decodeState('{"version":1,"stores":{"official":{"lastTab":"INSTALLED"}}}')
        self.assertEqual("INSTALLED", data["official"]["lastTab"])

    def test_malformed_settings_fall_back_without_splitting_strings(self):
        self.config.conf["serrebiStore"] = {
            "browseMemory": "broken", "browseAcrossRestarts": "yes",
            "columnOrder": "source,status", "hiddenColumns": ["status", 1, None],
        }
        self.assertEqual("default", self.module._getSetting("browseMemory"))
        self.assertFalse(self.module._getSetting("browseAcrossRestarts"))
        self.assertEqual([], self.module._getSetting("columnOrder"))
        self.assertEqual(["status"], self.module._getSetting("hiddenColumns"))

    def test_source_identity_unknown_and_unicode(self):
        self.assertEqual(self.module._UNKNOWN_SOURCE, self.module._sourceKey(types.SimpleNamespace()))
        model = types.SimpleNamespace(_serrebiStoreSource=" Éditions 中文 ")
        self.assertEqual("éditions 中文", self.module._sourceKey(model))

    def test_snapshot_uses_names_and_stable_id_not_row_number(self):
        vm = types.SimpleNamespace(
            _sortByModelField=types.SimpleNamespace(name="publicationDate"), _reverseSort=True,
            _filterString="தமிழ்", _serrebiSearchScope="title", _serrebiSources={"russian", "github"},
            selectedAddonId="someAddon-stable", _serrebiDateSort=True,
        )
        store = types.SimpleNamespace(listVM=vm, _filterChannelKey=types.SimpleNamespace(name="STABLE"),
                                      _filterEnabledDisabled=types.SimpleNamespace(name="ALL"),
                                      _filterIncludeIncompatible=False)
        state = self.module._snapshot(types.SimpleNamespace(_storeVM=store))
        self.assertEqual("publicationDate", state["sort"])
        self.assertEqual("someAddon-stable", state["selected"])
        self.assertEqual(["github", "russian"], state["sources"])
        self.assertEqual("தமிழ்", state["search"])
        for dateSort in (None, False, True):
            vm._serrebiDateSort = dateSort
            self.assertIs(dateSort, self.module._snapshot(types.SimpleNamespace(_storeVM=store))["dateSort"])
        self.assertTrue(state["dateSort"])

    def test_snapshot_keeps_pending_selection_during_loading_reset(self):
        vm = types.SimpleNamespace(
            _sortByModelField=types.SimpleNamespace(name="displayName"), _reverseSort=False,
            _filterString=None, selectedAddonId=None, _serrebiPendingSelection="saved-stable-id",
        )
        store = types.SimpleNamespace(listVM=vm, _filterChannelKey=types.SimpleNamespace(name="ALL"),
                                      _filterEnabledDisabled=types.SimpleNamespace(name="ALL"),
                                      _filterIncludeIncompatible=False)
        self.assertEqual("saved-stable-id", self.module._snapshot(types.SimpleNamespace(_storeVM=store))["selected"])

    def test_secure_and_unknown_context_fail_closed(self):
        globalVars = types.ModuleType("globalVars")
        globalVars.appArgs = types.SimpleNamespace(secure=True)
        with mock.patch.dict(sys.modules, {"globalVars": globalVars}):
            self.assertTrue(self.module._isSecure())
            globalVars.appArgs.secure = False
            self.assertFalse(self.module._isSecure())

    def test_native_sort_fields_remain_available_with_visual_hidden_columns(self):
        vm = types.SimpleNamespace(presentedFields=["name", "date"], sortableFields=["rank", "name", "date"])
        self.assertEqual(["rank", "name", "date"], self.module._sortFields(vm))

    def _makeAdapter(self):
        class Field(Enum):
            displayName = ("Name", 100)
            status = ("Status", 80)

            def __init__(self, displayString, width):
                self.displayString = displayString
                self.width = width

        class Channel(Enum):
            ALL = 1
            STABLE = 2

        class Enabled(Enum):
            ALL = 1

        class Action:
            def __init__(self):
                self.calls = 0

            def notify(self):
                self.calls += 1

        class ListVM:
            presentedFields = [Field.displayName, Field.status]
            sortableFields = presentedFields

            def resetListItems(self, items):
                self._addonsFilteredOrdered = list(items)

            def setSelection(self, index):
                self.selectedAddonId = None if index is None else self._addonsFilteredOrdered[index]

            def _getFilteredSortedIds(self):
                return list(self._addonsFilteredOrdered)

            def setSortField(self, field, reverse=False):
                self._sortByModelField = field
                self._reverseSort = reverse

            def applyFilter(self, text):
                self._filterString = text or None

        class Dialog:
            def __init__(self, *args, **kwargs):
                pass

            def onClose(self, evt):
                pass

            def onListTabPageChange(self, evt):
                self._storeVM.refresh()

            def _createFilterControls(self, helper):
                pass

            def onColumnFilterChange(self, evt):
                pass

            def onChannelFilterChange(self, evt):
                pass

            def onEnabledFilterChange(self, evt):
                pass

            def onIncompatibleFilterChange(self, evt):
                pass

            def onFilterTextChange(self, evt):
                pass

        class Control:
            def _refreshSelection(self):
                self._addonsListVM.setSelection(None)

            def _refreshColumns(self):
                pass

            def OnGetItemText(self, row, col):
                return f"native-{row}-{col}"

            def OnColClick(self, evt):
                pass

        class Settings:
            def makeSettings(self, sizer):
                pass

            def onSave(self):
                pass

        class Plugin:
            def __init__(self):
                self._sourceSupportPatches = []

            def _rememberPatch(self, owner, name, replacement):
                original = getattr(owner, name)
                setattr(owner, name, replacement)
                self._sourceSupportPatches.append((owner, name, original, replacement))

            def terminate(self):
                pass

        modules = {
            "gui.addonStoreGui.controls.storeDialog": types.SimpleNamespace(AddonStoreDialog=Dialog),
            "gui.addonStoreGui.viewModels.addonList": types.SimpleNamespace(
                AddonListVM=ListVM, AddonListField=Field,
            ),
            "gui.addonStoreGui.controls.addonList": types.SimpleNamespace(AddonVirtualList=Control),
            "addonStore.models.status": types.SimpleNamespace(EnabledStatus=Enabled, _statusFilters={}),
            "addonStore.models.channel": types.SimpleNamespace(
                Channel=Channel, _channelFilters={Channel.ALL: set(), Channel.STABLE: set()},
            ),
            "globalVars": types.SimpleNamespace(appArgs=types.SimpleNamespace(secure=False)),
            "NVDAState": types.SimpleNamespace(shouldWriteToDisk=lambda: True),
        }
        self.config.conf = {
            "serrebiStore": {"browseMemory": "perTab", "rememberPosition": True},
            "addonStore": {"baseServerURL": "mirror"},
        }
        self.config.conf = type("Conf", (dict,), {})(self.config.conf)
        self.config.conf.spec = {"serrebiStore": {}}
        return modules, Plugin(), Dialog, ListVM, Settings, Field, Channel, Enabled, Action

    def test_adapter_restores_selection_and_user_intervention_cancels_pending(self):
        modules, plugin, _, ListVM, Settings, _, _, _, Action = self._makeAdapter()
        with mock.patch.dict(sys.modules, modules):
            self.module.enable(plugin, Settings)
            vm = ListVM()
            vm._addonsFilteredOrdered = []
            vm._addons = {}
            vm._isLoading = False
            vm.updated = Action()
            vm._serrebiPendingSelection = "saved"
            vm.resetListItems(["first", "saved"])
            self.assertEqual("saved", vm.selectedAddonId)
            self.assertEqual(1, vm.updated.calls)
            vm._serrebiPendingSelection = "saved"
            vm.setSelection(0)
            self.assertIsNone(vm._serrebiPendingSelection)
            self.assertEqual("first", vm.selectedAddonId)

    def test_core_loading_selection_event_does_not_cancel_saved_selection(self):
        modules, plugin, _, ListVM, Settings, _, _, _, _ = self._makeAdapter()
        with mock.patch.dict(sys.modules, modules):
            self.module.enable(plugin, Settings)
            vm = ListVM()
            vm._addonsFilteredOrdered = []
            vm._serrebiPendingSelection = "saved"
            control = modules["gui.addonStoreGui.controls.addonList"].AddonVirtualList()
            control._addonsListVM = vm
            control._refreshSelection()
            self.assertEqual("saved", vm._serrebiPendingSelection)
            vm.setSelection(None)
            self.assertIsNone(vm._serrebiPendingSelection)

    def test_adapter_persists_session_state_when_restart_memory_is_enabled_later(self):
        modules, plugin, Dialog, ListVM, Settings, Field, Channel, Enabled, Action = self._makeAdapter()
        with mock.patch.dict(sys.modules, modules):
            self.module.enable(plugin, Settings)
            vm = ListVM()
            vm._addonsFilteredOrdered = ["saved"]
            vm._addons = {}
            vm.updated = Action()
            vm._sortByModelField = Field.displayName
            vm._reverseSort = False
            vm._filterString = None
            vm._serrebiSources = None
            vm.selectedAddonId = "saved"
            store = types.SimpleNamespace(
                listVM=vm, _filteredStatusKey=types.SimpleNamespace(name="AVAILABLE"),
                _filterChannelKey=Channel.STABLE, _filterEnabledDisabled=Enabled.ALL,
                _filterIncludeIncompatible=False,
            )
            dialog = object.__new__(Dialog)
            dialog._storeVM = store
            dialog._serrebiCurrentTab = "AVAILABLE"
            dialog.onChannelFilterChange(None)
            self.assertNotIn("browseState", self.config.conf["serrebiStore"])

            self.config.conf["serrebiStore"]["browseAcrossRestarts"] = True
            panel = Settings()
            panel._browseMode = types.SimpleNamespace(GetSelection=lambda: 2)
            panel._browseChecks = {
                "browseAcrossRestarts": types.SimpleNamespace(IsChecked=lambda: True),
                "rememberTab": types.SimpleNamespace(IsChecked=lambda: False),
                "rememberPosition": types.SimpleNamespace(IsChecked=lambda: True),
            }
            panel._columnNames = ["displayName", "status"]
            panel._columnsList = types.SimpleNamespace(IsChecked=lambda index: True)
            panel.onSave()
            state = json.loads(self.config.conf["serrebiStore"]["browseState"])
            self.assertEqual("saved", state["stores"]["mirror"]["tabs"]["AVAILABLE"]["selected"])

    def test_dialog_callbacks_do_not_keep_destroyed_store_alive(self):
        modules, plugin, Dialog, ListVM, Settings, Field, Channel, Enabled, Action = self._makeAdapter()
        with mock.patch.dict(sys.modules, modules):
            self.module.enable(plugin, Settings)
            vm = ListVM()
            vm._addonsFilteredOrdered = []
            vm._addons = {}
            vm.updated = Action()
            vm._sortByModelField = Field.displayName
            vm._reverseSort = False
            vm._filterString = None
            vm._serrebiSources = None
            vm.selectedAddonId = None
            store = types.SimpleNamespace(
                listVM=vm, _filteredStatusKey=types.SimpleNamespace(name="AVAILABLE"),
                _filterChannelKey=Channel.STABLE, _filterEnabledDisabled=Enabled.ALL,
                _filterIncludeIncompatible=False,
            )
            dialog = object.__new__(Dialog)
            dialog._storeVM = store
            Dialog.__init__(dialog)
            callback = dialog._serrebiSaveBrowsing
            dialogRef = weakref.ref(dialog)
            del dialog
            self.assertIsNone(dialogRef())
            # A feature layered outside browsing may retain this callback.
            # It still cannot retain the destroyed dialog.
            callback()

    def test_source_filter_button_handler_does_not_keep_store_alive(self):
        modules, plugin, Dialog, ListVM, Settings, Field, Channel, Enabled, Action = self._makeAdapter()
        with mock.patch.dict(sys.modules, modules):
            self.module.enable(plugin, Settings)
            vm = ListVM()
            vm._addonsFilteredOrdered = []
            vm._addons = {}
            vm.updated = Action()
            vm._sortByModelField = Field.displayName
            vm._reverseSort = False
            vm._filterString = None
            vm._serrebiSources = None
            vm.selectedAddonId = None
            store = types.SimpleNamespace(
                listVM=vm, _filteredStatusKey=types.SimpleNamespace(name="AVAILABLE"),
                _filterChannelKey=Channel.STABLE, _filterEnabledDisabled=Enabled.ALL,
                _filterIncludeIncompatible=False,
            )
            dialog = object.__new__(Dialog)
            dialog._storeVM = store
            Dialog.__init__(dialog)
            buttons = []
            with mock.patch.object(builtins, "_", lambda text: text, create=True):
                dialog._createFilterControls(types.SimpleNamespace(addItem=buttons.append))
            dialogRef = weakref.ref(dialog)
            handler = buttons[0].handler
            del dialog
            self.assertIsNone(dialogRef())
            handler(None)

    def test_user_selection_is_saved_without_waiting_for_a_tab_change(self):
        modules, plugin, Dialog, ListVM, Settings, Field, Channel, Enabled, Action = self._makeAdapter()
        self.config.conf["serrebiStore"]["browseAcrossRestarts"] = True
        with mock.patch.dict(sys.modules, modules):
            self.module.enable(plugin, Settings)
            vm = ListVM()
            vm._addonsFilteredOrdered = ["first", "saved"]
            vm._addons = {}
            vm.updated = Action()
            vm._sortByModelField = Field.displayName
            vm._reverseSort = False
            vm._filterString = None
            vm._serrebiSources = None
            vm.selectedAddonId = None
            store = types.SimpleNamespace(
                listVM=vm, _filteredStatusKey=types.SimpleNamespace(name="AVAILABLE"),
                _filterChannelKey=Channel.STABLE, _filterEnabledDisabled=Enabled.ALL,
                _filterIncludeIncompatible=False,
            )
            dialog = object.__new__(Dialog)
            dialog._storeVM = store
            Dialog.__init__(dialog)
            vm.setSelection(1)
            state = json.loads(self.config.conf["serrebiStore"]["browseState"])
            self.assertEqual("saved", state["stores"]["mirror"]["tabs"]["AVAILABLE"]["selected"])

    def test_adapter_refreshes_once_after_tab_defaults_and_restore(self):
        modules, plugin, Dialog, ListVM, Settings, Field, Channel, Enabled, Action = self._makeAdapter()
        self.config.conf["serrebiStore"].update({"browseMemory": "default", "rememberPosition": False})
        with mock.patch.dict(sys.modules, modules):
            self.module.enable(plugin, Settings)
            calls = []
            vm = ListVM()
            vm._addonsFilteredOrdered = []
            vm._addons = {}
            vm.updated = Action()
            vm._serrebiSources = None
            store = types.SimpleNamespace(
                listVM=vm, _filteredStatusKey=types.SimpleNamespace(name="AVAILABLE"),
                refresh=lambda: calls.append("refresh"),
            )
            dialog = object.__new__(Dialog)
            dialog._storeVM = store
            dialog.onListTabPageChange(None)
            self.assertEqual(["refresh"], calls)

    def test_adapter_restores_date_sort_only_when_saved_state_has_it(self):
        modules, plugin, Dialog, ListVM, Settings, Field, Channel, Enabled, Action = self._makeAdapter()

        class Choice:
            def __init__(self, count=1):
                self.count = count
                self.selection = None

            def GetCount(self):
                return self.count

            def SetSelection(self, value):
                self.selection = value

        class Text:
            def ChangeValue(self, value):
                self.value = value

        class Check:
            def SetValue(self, value):
                self.value = value

        def run(state):
            self.config.conf["serrebiStore"].update({
                "browseMemory": "perTab", "browseAcrossRestarts": bool(state),
                "browseState": json.dumps({"stores": {"mirror": {"tabs": {"AVAILABLE": state}}}}),
            })
            with mock.patch.dict(sys.modules, modules):
                self.module.enable(plugin, Settings)
                vm = ListVM()
                vm._addonsFilteredOrdered = []
                vm._addons = {}
                vm.updated = Action()
                vm._sortByModelField = Field.displayName
                vm._reverseSort = False
                vm._filterString = None
                vm._serrebiSources = None
                vm._serrebiDateSort = True
                vm._serrebiSearchScope = "all"
                store = types.SimpleNamespace(
                    listVM=vm, _filteredStatusKey=types.SimpleNamespace(name="AVAILABLE"),
                    _filterChannelKey=Channel.STABLE, _filterEnabledDisabled=Enabled.ALL,
                    _filterIncludeIncompatible=False,
                    refresh=lambda: None,
                )
                dialog = object.__new__(Dialog)
                dialog._storeVM = store
                dialog._serrebiApplyTabDefaults = lambda: setattr(vm, "_serrebiDateSort", True)
                dialog.channelFilterCtrl = Choice(len(modules["addonStore.models.channel"]._channelFilters))
                dialog.enabledFilterCtrl = Choice(len(Enabled))
                dialog.includeIncompatibleCtrl = Check()
                dialog.searchFilterCtrl = Text()
                dialog.columnFilterCtrl = Choice(len(vm.sortableFields) * 2 + 2)
                dialog._setListLabels = lambda: None
                dialog.onListTabPageChange(None)
                return vm._serrebiDateSort

        self.assertTrue(run({}))
        self.assertIsNone(run({"dateSort": None}))

    def test_adapter_source_filter_composes_with_native_order(self):
        modules, plugin, _, ListVM, Settings, _, _, _, _ = self._makeAdapter()
        with mock.patch.dict(sys.modules, modules):
            self.module.enable(plugin, Settings)
            vm = ListVM()
            vm._addonsFilteredOrdered = ["official", "unknown", "github"]
            vm._addons = {
                "official": types.SimpleNamespace(
                    model=types.SimpleNamespace(_serrebiStoreSource="Official"),
                ),
                "unknown": types.SimpleNamespace(model=types.SimpleNamespace()),
                "github": types.SimpleNamespace(model=types.SimpleNamespace(_serrebiStoreSource="GitHub")),
            }
            vm._serrebiSources = {"github", self.module._UNKNOWN_SOURCE}
            self.assertEqual(["unknown", "github"], vm._getFilteredSortedIds())
            vm._serrebiSources = set()
            self.assertEqual([], vm._getFilteredSortedIds())
