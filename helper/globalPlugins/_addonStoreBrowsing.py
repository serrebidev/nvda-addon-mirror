"""Store-separated browsing memory, source filtering and spoken column order."""

import importlib
import json
import weakref
from typing import Any

import wx
import addonHandler
import config

addonHandler.initTranslation()

_UNKNOWN_SOURCE = "__unknown__"
_MEMORY_MODES = ("default", "shared", "perTab")
_STATE_LIMIT = 64
_SPEC = {
	"browseMemory": "option('default', 'shared', 'perTab', default='default')",
	"browseAcrossRestarts": "boolean(default=False)",
	"rememberTab": "boolean(default=False)",
	"rememberPosition": "boolean(default=False)",
	"browseState": "string(default='{}')",
	"columnOrder": "string_list(default=list())",
	"hiddenColumns": "string_list(default=list())",
}


def _isSecure() -> bool:
	try:
		import globalVars
		return bool(globalVars.appArgs.secure)
	except (ImportError, AttributeError):
		return True


def _getSetting(name: str) -> Any:
	defaults = {
		"browseMemory": "default", "browseAcrossRestarts": False,
		"rememberTab": False, "rememberPosition": False, "browseState": "{}",
		"columnOrder": [], "hiddenColumns": [],
	}
	try:
		value = config.conf["serrebiStore"].get(name, defaults[name])
	except (KeyError, TypeError, AttributeError):
		return defaults[name]
	if name == "browseMemory":
		return value if value in _MEMORY_MODES else defaults[name]
	if name in ("browseAcrossRestarts", "rememberTab", "rememberPosition"):
		return value if isinstance(value, bool) else defaults[name]
	if name == "browseState":
		return value if isinstance(value, str) else defaults[name]
	if name in ("columnOrder", "hiddenColumns"):
		return [item for item in value if isinstance(item, str)] if isinstance(value, (list, tuple)) else []
	return value


def _decodeState(text: str) -> dict:
	try:
		data = json.loads(text) if len(text) <= 262144 else {}
		if not isinstance(data, dict) or data.get("version", 1) != 1:
			return {}
		stores = data.get("stores", {})
		result = {}
		for key, value in list(stores.items())[-_STATE_LIMIT:]:
			if not isinstance(value, dict):
				continue
			tabs = value.get("tabs", {})
			result[key] = {
				"lastTab": value.get("lastTab"),
				"shared": value.get("shared") if isinstance(value.get("shared"), dict) else {},
				"tabs": {name: snapshot for name, snapshot in tabs.items() if isinstance(snapshot, dict)}
				if isinstance(tabs, dict) else {},
			}
		return result
	except (TypeError, ValueError, AttributeError):
		return {}


def _sourceKey(model: Any) -> str:
	text = getattr(model, "_serrebiStoreSource", "")
	return text.strip().casefold() if isinstance(text, str) and text.strip() else _UNKNOWN_SOURCE


def _layoutFields(fields: list, order: list[str], hidden: list[str]) -> list:
	"""Return visual map without modifying native fields or validation."""
	byName = {field.name: field for field in fields}
	byName["source"] = "source"
	hidden = set(hidden) - {"displayName"}
	names = []
	for name in list(order) + list(byName):
		if name in byName and name not in hidden and name not in names:
			names.append(name)
	return [byName[name] for name in names]


def _snapshot(dialog: Any) -> dict:
	store = dialog._storeVM
	vm = store.listVM
	selected = vm.selectedAddonId
	# A background refresh temporarily clears the native selection. Keep the
	# stable target until its pending restore has either completed or been
	# cancelled by an actual user selection.
	if selected is None and isinstance(getattr(vm, "_serrebiPendingSelection", None), str):
		selected = vm._serrebiPendingSelection
	return {
		"sort": vm._sortByModelField.name, "reverse": bool(vm._reverseSort),
		"search": vm._filterString or "", "scope": getattr(vm, "_serrebiSearchScope", "all"),
		"channel": store._filterChannelKey.name, "enabled": store._filterEnabledDisabled.name,
		"incompatible": bool(store._filterIncludeIncompatible),
		"sources": sorted(vm._serrebiSources) if getattr(vm, "_serrebiSources", None) is not None else None,
		"selected": selected,
		"dateSort": getattr(vm, "_serrebiDateSort", None),
	}


def _sortFields(vm: Any) -> list:
	return list(getattr(vm, "sortableFields", vm.presentedFields))


class SourceFilterDialog(wx.Dialog):
	def __init__(self, parent: Any, viewModel: Any):
		# Translators: Dialog for choosing which catalog sources to display.
		super().__init__(parent, title=_("Filter add-on sources"))
		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Source filter for add-ons without catalog provenance.
		labels = {_UNKNOWN_SOURCE: _("Unknown source")}
		for item in viewModel._addons.values():
			key = _sourceKey(item.model)
			# Translators: Add-ons whose cached metadata does not name a catalog source.
			labels[key] = getattr(item.model, "_serrebiStoreSource", "") or _("Unknown source")
		self.keys = sorted(labels)
		# Translators: Display all catalog sources, including newly loaded sources.
		self.allCheck = wx.CheckBox(self, label=_("Show &all sources"))
		self.allCheck.SetValue(getattr(viewModel, "_serrebiSources", None) is None)
		sizer.Add(self.allCheck, flag=wx.ALL, border=8)
		# Translators: Label for the list of individual source checkboxes.
		sizer.Add(wx.StaticText(self, label=_("&Sources to show:")), flag=wx.LEFT, border=8)
		self.list = wx.CheckListBox(self, choices=[labels[key] for key in self.keys])
		self.list.SetMinSize((440, 210))
		selected = getattr(viewModel, "_serrebiSources", None)
		for index, key in enumerate(self.keys):
			self.list.Check(index, selected is None or key in selected)
		self.list.Enable(not self.allCheck.IsChecked())
		self.allCheck.Bind(wx.EVT_CHECKBOX, lambda evt: self.list.Enable(not self.allCheck.IsChecked()))
		sizer.Add(self.list, proportion=1, flag=wx.EXPAND | wx.ALL, border=8)
		sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), flag=wx.ALL, border=8)
		self.SetSizerAndFit(sizer)


def enable(plugin: Any, settingsPanel: Any) -> None:
	if _isSecure():
		return
	config.conf.spec["serrebiStore"].update(_SPEC)
	dialogClass = importlib.import_module("gui.addonStoreGui.controls.storeDialog").AddonStoreDialog
	listModule = importlib.import_module("gui.addonStoreGui.viewModels.addonList")
	listClass = listModule.AddonListVM
	controlClass = importlib.import_module("gui.addonStoreGui.controls.addonList").AddonVirtualList
	statusModule = importlib.import_module("addonStore.models.status")
	channelModule = importlib.import_module("addonStore.models.channel")
	start = len(plugin._sourceSupportPatches)
	states = _decodeState(_getSetting("browseState")) if _getSetting("browseAcrossRestarts") else {}
	dialogs = weakref.WeakSet()
	active = [True]

	def persist():
		if not _getSetting("browseAcrossRestarts"):
			return
		import NVDAState
		if NVDAState.shouldWriteToDisk():
			config.conf["serrebiStore"]["browseState"] = json.dumps(
				{"version": 1, "stores": dict(list(states.items())[-_STATE_LIMIT:])},
				ensure_ascii=False,
			)

	def keyFor(dialog):
		return getattr(dialog, "_serrebiStoreKey", config.conf["addonStore"]["baseServerURL"])

	def save(dialog):
		if _isSecure() or getattr(dialog, "_serrebiRestoring", False):
			return
		key = keyFor(dialog)
		storeState = states.setdefault(key, {"tabs": {}})
		tab = getattr(dialog, "_serrebiCurrentTab", dialog._storeVM._filteredStatusKey.name)
		storeState["lastTab"] = tab
		snapshot = _snapshot(dialog)
		storeState.setdefault("tabs", {})[tab] = snapshot
		storeState["shared"] = snapshot
		persist()

	def saveFromVM(vm):
		"""Save a user selection without making a Store VM retain its dialog."""
		dialogRef = getattr(vm, "_serrebiBrowsingDialogRef", None)
		dialog = dialogRef() if callable(dialogRef) else None
		if dialog is not None:
			save(dialog)

	def restorePending(vm):
		if not active[0] or _isSecure():
			return
		addonId = getattr(vm, "_serrebiPendingSelection", None)
		if addonId in vm._addonsFilteredOrdered:
			vm._serrebiPendingSelection = None
			vm._serrebiApplyingSelection = True
			try:
				vm.setSelection(vm._addonsFilteredOrdered.index(addonId))
			finally:
				vm._serrebiApplyingSelection = False
			# resetListItems may already have notified the virtual list before this
			# delayed selection was applied. Notify it again so visual and model
			# selection cannot diverge based on queue ordering.
			vm.updated.notify()
		elif not getattr(vm, "_isLoading", False):
			vm._serrebiPendingSelection = None

	def restore(dialog, refresh):
		store = dialog._storeVM
		vm = store.listVM
		storeState = states.get(keyFor(dialog), {})
		tab = store._filteredStatusKey.name
		mode = _getSetting("browseMemory")
		state = storeState.get("shared", {}) if mode == "shared" else storeState.get("tabs", {}).get(tab, {})
		if not isinstance(state, dict):
			state = {}
		dialog._serrebiRestoring = True
		try:
			if mode != "default":
				channels = list(channelModule._channelFilters)
				for index, channel in enumerate(channels):
					if channel.name == state.get("channel") and index < dialog.channelFilterCtrl.GetCount():
						store._filterChannelKey = channel
						dialog.channelFilterCtrl.SetSelection(index)
				for index, enabled in enumerate(statusModule.EnabledStatus):
					if enabled.name == state.get("enabled"):
						store._filterEnabledDisabled = enabled
						dialog.enabledFilterCtrl.SetSelection(index)
				store._filterIncludeIncompatible = bool(state.get("incompatible", False))
				dialog.includeIncompatibleCtrl.SetValue(store._filterIncludeIncompatible)
				sources = state.get("sources")
				vm._serrebiSources = {source for source in sources if isinstance(source, str)} \
					if isinstance(sources, list) else None
				text = state.get("search", "")
				text = text if isinstance(text, str) else ""
				dialog.searchFilterCtrl.ChangeValue(text)
				scopes = ("all", "title", "author", "description", "id", "source")
				vm._serrebiSearchScope = state.get("scope") if state.get("scope") in scopes else "all"
				if hasattr(dialog, "_serrebiSearchScopeCtrl"):
					dialog._serrebiSearchScopeCtrl.SetSelection(scopes.index(vm._serrebiSearchScope))
				vm.applyFilter(text)
				sortFields = _sortFields(vm)
				for index, field in enumerate(sortFields):
					if field.name == state.get("sort"):
						vm.setSortField(field, bool(state.get("reverse")))
						dialog.columnFilterCtrl.SetSelection(index * 2 + bool(state.get("reverse")))
				dateSort = state.get("dateSort")
				dateSort = dateSort if type(dateSort) is bool else None
				if dialog.columnFilterCtrl.GetCount() >= len(sortFields) * 2 + 2:
					vm._serrebiDateSort = dateSort
					if dateSort is not None:
						dialog.columnFilterCtrl.SetSelection(len(sortFields) * 2 + int(dateSort))
				dialog._setListLabels()
			if _getSetting("rememberPosition"):
				positionState = storeState.get("tabs", {}).get(tab, {})
				vm._serrebiPendingSelection = positionState.get("selected")
			refresh()
			if _getSetting("rememberPosition"):
				if tab not in ("AVAILABLE", "UPDATE"):
					wx.CallAfter(restorePending, vm)
		finally:
			dialog._serrebiRestoring = False

	originalTabChange = dialogClass.onListTabPageChange
	originalInit = dialogClass.__init__
	originalControls = dialogClass._createFilterControls
	originalColumnChange = dialogClass.onColumnFilterChange
	originalChannelChange = dialogClass.onChannelFilterChange
	originalEnabledChange = dialogClass.onEnabledFilterChange
	originalIncompatibleChange = dialogClass.onIncompatibleFilterChange
	originalSearchChange = dialogClass.onFilterTextChange
	originalClose = dialogClass.onClose
	originalReset = listClass.resetListItems
	originalSetSelection = listClass.setSelection
	originalFilter = listClass._getFilteredSortedIds
	originalColumns = controlClass._refreshColumns
	originalText = controlClass.OnGetItemText
	originalClick = controlClass.OnColClick
	originalRefreshSelection = getattr(controlClass, "_refreshSelection", None)
	originalSettings = settingsPanel.makeSettings
	originalSave = settingsPanel.onSave
	originalTerminate = plugin.terminate

	def initDialog(dialog, *args, **kwargs):
		# These callbacks are owned by the wx dialog.  They must not close over it:
		# SettingsDialog deliberately keeps destroyed instances observable until all
		# strong references are gone, and a callback -> dialog cycle prevents the
		# Store from being opened again.
		dialogRef = weakref.ref(dialog)
		def saveReferencedDialog():
			dialog = dialogRef()
			if dialog is not None:
				save(dialog)
		dialog._serrebiSaveBrowsing = saveReferencedDialog
		key = config.conf["addonStore"]["baseServerURL"]
		dialog._serrebiStoreKey = key
		if _getSetting("rememberTab"):
			name = states.get(key, {}).get("lastTab")
			if len(args) <= 2 and kwargs.get("openToTab") is None:
				for tab in statusModule._statusFilters:
					if tab.name == name:
						kwargs["openToTab"] = tab
		originalInit(dialog, *args, **kwargs)
		dialogs.add(dialog)
		dialog._storeVM.listVM._serrebiBrowsingDialogRef = dialogRef

	def tabChange(dialog, evt):
		if hasattr(dialog, "_serrebiCurrentTab"):
			save(dialog)
		dialog._storeVM.listVM._serrebiSources = None
		dialog._storeVM.listVM._serrebiPendingSelection = None
		store = dialog._storeVM
		refresh = store.refresh
		missing = object()
		ownRefresh = getattr(store, "__dict__", {}).get("refresh", missing)
		# Core refreshes immediately after resetting tab defaults. Suppress that
		# intermediate fetch and refresh once, after the saved filters are restored.
		store.refresh = lambda: None
		dialog._serrebiRestoring = True
		try:
			originalTabChange(dialog, evt)
			# Deferred search can swallow the programmatic EVT_TEXT generated by
			# core's SetValue(""). Apply the cleared filter and native sort reset.
			if getattr(store.listVM, "_filterString", None) and not dialog.searchFilterCtrl.GetValue():
				originalSearchChange(dialog, evt)
		finally:
			dialog._serrebiRestoring = False
			if ownRefresh is missing:
				del store.refresh
			else:
				store.refresh = ownRefresh
		dialog._serrebiCurrentTab = dialog._storeVM._filteredStatusKey.name
		restore(dialog, refresh)

	def saveAfter(original):
		def wrapper(dialog, evt):
			result = original(dialog, evt)
			save(dialog)
			return result
		return wrapper

	def saveBeforeClose(dialog, evt):
		# This is bound by SettingsDialog during construction.  Saving here keeps
		# the controls valid and also covers a Store closed without a tab change.
		save(dialog)
		return originalClose(dialog, evt)

	def searchChange(dialog, evt):
		before = dialog._storeVM.listVM._filterString
		result = originalSearchChange(dialog, evt)
		# Deferred-search mode deliberately swallows EVT_TEXT. Persist only the
		# effective VM filter after core has actually applied it.
		if dialog._storeVM.listVM._filterString != before:
			save(dialog)
		return result

	def reset(vm, items):
		originalReset(vm, items)
		if getattr(vm, "_serrebiPendingSelection", None):
			wx.CallAfter(restorePending, vm)

	def setSelection(vm, index):
		applying = getattr(vm, "_serrebiApplyingSelection", False)
		if not applying:
			vm._serrebiPendingSelection = None
		result = originalSetSelection(vm, index)
		# User selection is otherwise only captured when another filter or tab is
		# changed.  Record it immediately, while ignoring native list refreshes.
		if not applying:
			saveFromVM(vm)
		return result

	def refreshSelection(control):
		vm = control._addonsListVM
		previous = getattr(vm, "_serrebiApplyingSelection", False)
		# Core emits selection events while synchronizing its list, including
		# deselection for an empty loading list. Those are not user intervention.
		vm._serrebiApplyingSelection = True
		try:
			return originalRefreshSelection(control)
		finally:
			vm._serrebiApplyingSelection = previous

	def filtered(vm):
		ordered = originalFilter(vm)
		sources = getattr(vm, "_serrebiSources", None)
		return ordered if sources is None else [
			addonId for addonId in ordered if _sourceKey(vm._addons[addonId].model) in sources
		]

	def columns(control):
		originalColumns(control)
		control._serrebiColumnMap = _layoutFields(
			control._addonsListVM.presentedFields, _getSetting("columnOrder"), _getSetting("hiddenColumns"),
		)
		control.ClearAll()
		for field in control._serrebiColumnMap:
			if field == "source":
				# Translators: The upstream catalog source column in the Add-on Store.
				label, width = _("Source"), 140
			else:
				label, width = field.displayString, field.width
			control.InsertColumn(control.GetColumnCount(), label, width=control.scaleSize(width))
		control.Layout()

	def itemText(control, row, col):
		try:
			field = control._serrebiColumnMap[col]
			if field == "source":
				item = control._addonsListVM.getAddonAtIndex(row)
				return getattr(item.model, "_serrebiStoreSource", "")
			return originalText(control, row, control._addonsListVM.presentedFields.index(field))
		except (IndexError, KeyError, AssertionError, ValueError):
			return ""

	def columnClick(control, evt):
		if not hasattr(control, "_serrebiColumnMap"):
			return originalClick(control, evt)
		field = control._serrebiColumnMap[evt.GetColumn()]
		if field == "source":
			return
		vm = control._addonsListVM
		reverse = not vm._reverseSort if vm._sortByModelField == field else False
		vm.setSortField(field, reverse)
		parent = control.GetParent()
		if hasattr(parent, "columnFilterCtrl"):
			parent.columnFilterCtrl.SetSelection(_sortFields(vm).index(field) * 2 + reverse)

	def createControls(dialog, *args, **kwargs):
		originalControls(dialog, *args, **kwargs)
		helper = args[0] if args else kwargs.get("filterCtrlHelper")
		if helper is None:
			return
		# Translators: Opens the native checklist of catalog sources to display.
		button = wx.Button(dialog, label=_("Filter so&urces..."))
		helper.addItem(button)
		dialogRef = weakref.ref(dialog)
		def choose(evt):
			if _isSecure():
				return
			dialog = dialogRef()
			if dialog is None or not dialog:
				return
			with SourceFilterDialog(dialog, dialog._storeVM.listVM) as chooser:
				if chooser.ShowModal() == wx.ID_OK:
					vm = dialog._storeVM.listVM
					vm._serrebiPendingSelection = None
					vm._serrebiSources = None if chooser.allCheck.IsChecked() else {
						key for index, key in enumerate(chooser.keys) if chooser.list.IsChecked(index)
						}
					vm.applyFilter(vm._filterString or "")
					save(dialog)
		button.Bind(wx.EVT_BUTTON, choose)

	def makeSettings(panel, sizer):
		originalSettings(panel, sizer)
		from gui import guiHelper
		helper = guiHelper.BoxSizerHelper(panel, sizer=sizer)
		# Translators: Controls whether browsing filters reset, are shared or are kept per tab.
		panel._browseMode = helper.addLabeledControl(_("Browsing &memory:"), wx.Choice, choices=[
			# Translators: Reset store filters to the defaults.
			_("Always use defaults"),
			# Translators: Share last-used filters across the store tabs.
			_("Shared across tabs"),
			# Translators: Keep separate filters for each store tab.
			_("Separate for each tab"),
		])
		panel._browseMode.SetSelection(_MEMORY_MODES.index(_getSetting("browseMemory")))
		panel._browseChecks = {}
		for key, label in (
			# Translators: Persist browsing memory when NVDA configuration is saved.
			("browseAcrossRestarts", _("Remember browsing across &restarts")),
			# Translators: Open the store to its most recently used tab.
			("rememberTab", _("Remember the last &tab")),
			# Translators: Restore the selected add-on by its stable identifier.
			("rememberPosition", _("Remember the selected add-on &position")),
		):
			check = wx.CheckBox(panel, label=label)
			check.SetValue(bool(_getSetting(key)))
			helper.addItem(check)
			panel._browseChecks[key] = check
		fields = [field for field in listModule.AddonListField if field.name != "searchRank"]
		byName = {field.name: field.displayString for field in fields}
		# Translators: The catalog-source column can be shown, hidden or moved.
		byName["source"] = _("Source")
		panel._columnNames = [
			field if isinstance(field, str) else field.name
			for field in _layoutFields(fields, _getSetting("columnOrder"), [])
		]
		# Translators: Settings checklist; checked columns are spoken/shown in this order.
		helper.addItem(wx.StaticText(panel, label=_("&Columns to show, in announcement order:")))
		panel._columnsList = wx.CheckListBox(panel, choices=[byName[name] for name in panel._columnNames])
		for index, name in enumerate(panel._columnNames):
			panel._columnsList.Check(index, name not in _getSetting("hiddenColumns") or name == "displayName")
		def onColumnCheck(evt):
			index = evt.GetInt()
			if panel._columnNames[index] == "displayName" and not panel._columnsList.IsChecked(index):
				panel._columnsList.Check(index, True)
				import ui
				# Translators: Status spoken when the required Name column is unchecked.
				ui.message(_("The Name column must remain visible."))
		panel._columnsList.Bind(wx.EVT_CHECKLISTBOX, onColumnCheck)
		helper.addItem(panel._columnsList)
		def move(delta):
			index = panel._columnsList.GetSelection()
			other = index + delta
			if index < 0 or not 0 <= other < len(panel._columnNames):
				return
			checked = {
				panel._columnNames[i] for i in range(len(panel._columnNames)) if panel._columnsList.IsChecked(i)
			}
			panel._columnNames[index], panel._columnNames[other] = panel._columnNames[other], panel._columnNames[index]
			panel._columnsList.Set([byName[name] for name in panel._columnNames])
			for i, name in enumerate(panel._columnNames):
				panel._columnsList.Check(i, name in checked)
			panel._columnsList.SetSelection(other)
			import ui
			# Translators: Spoken after moving a store column. The first value is the column name,
			# the second its one-based position and the third the total number of columns.
			ui.message(_("{column}, position {position} of {total}").format(
				column=byName[panel._columnNames[other]], position=other + 1, total=len(panel._columnNames),
			))
		for delta, label in (
			# Translators: Moves the selected column earlier in spoken/visual order.
			(-1, _("Move &up")),
			# Translators: Moves the selected column later in spoken/visual order.
			(1, _("Move &down")),
		):
			button = wx.Button(panel, label=label)
			button.Bind(wx.EVT_BUTTON, lambda evt, delta=delta: move(delta))
			helper.addItem(button)

	def saveSettings(panel):
		originalSave(panel)
		if _isSecure():
			return
		settings = config.conf["serrebiStore"]
		settings["browseMemory"] = _MEMORY_MODES[panel._browseMode.GetSelection()]
		for key, check in panel._browseChecks.items():
			settings[key] = check.IsChecked()
		settings["columnOrder"] = panel._columnNames
		settings["hiddenColumns"] = [
			name for index, name in enumerate(panel._columnNames)
			if not panel._columnsList.IsChecked(index) and name != "displayName"
		]
		persist()
		for dialog in list(dialogs):
			try:
				isBeingDeleted = getattr(dialog, "IsBeingDeleted", None)
				if not dialog or (callable(isBeingDeleted) and isBeingDeleted()):
					continue
			except RuntimeError:
				continue
			dialog.addonListView._refreshColumns()

	def terminate():
		active[0] = False
		originalTerminate()

	try:
		if originalRefreshSelection is not None:
			plugin._rememberPatch(controlClass, "_refreshSelection", refreshSelection)
		for owner, name, replacement in (
			(dialogClass, "__init__", initDialog), (dialogClass, "onClose", saveBeforeClose),
			(dialogClass, "onListTabPageChange", tabChange),
			(dialogClass, "_createFilterControls", createControls),
			(dialogClass, "onColumnFilterChange", saveAfter(originalColumnChange)),
			(dialogClass, "onChannelFilterChange", saveAfter(originalChannelChange)),
			(dialogClass, "onEnabledFilterChange", saveAfter(originalEnabledChange)),
			(dialogClass, "onIncompatibleFilterChange", saveAfter(originalIncompatibleChange)),
			(dialogClass, "onFilterTextChange", searchChange),
			(listClass, "resetListItems", reset), (listClass, "setSelection", setSelection),
			(listClass, "_getFilteredSortedIds", filtered),
			(controlClass, "_refreshColumns", columns), (controlClass, "OnGetItemText", itemText),
			(controlClass, "OnColClick", columnClick), (settingsPanel, "makeSettings", makeSettings),
			(settingsPanel, "onSave", saveSettings), (plugin, "terminate", terminate),
		):
			plugin._rememberPatch(owner, name, replacement)
	except Exception:
		active[0] = False
		for owner, name, original, replacement in reversed(plugin._sourceSupportPatches[start:]):
			if owner.__dict__.get(name) is replacement:
				setattr(owner, name, original)
		del plugin._sourceSupportPatches[start:]
		raise
