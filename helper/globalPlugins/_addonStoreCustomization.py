"""Semantic Store defaults, ordered tabs/sorts and native-model favourites."""
import builtins
import importlib
import json
import weakref

import config
import wx

_ = getattr(builtins, "_", lambda text: text)
TABS = ("INSTALLED", "UPDATE", "AVAILABLE", "INCOMPATIBLE", "FAVOURITES")
FAVOURITES = "FAVOURITES"
_SPEC = {
	"tabOrder": "string_list(default=list())",
	"sortOrder": "string_list(default=list())",
	"tabDefaults": "string(default='{}')",
	"sortOnEnter": "boolean(default=False)",
	"contextualColumns": "boolean(default=False)",
	"favourites": "string_list(default=list())",
}


def _secure():
	try:
		import globalVars
		return bool(globalVars.appArgs.secure)
	except (ImportError, AttributeError):
		return True


def _setting(name, default):
	try:
		return config.conf["serrebiStore"].get(name, default)
	except (KeyError, AttributeError, TypeError):
		return default


def orderedKeys(saved, available):
	saved = saved if isinstance(saved, (list, tuple)) else []
	saved = [_dateAlias(key) for key in saved]
	return list(dict.fromkeys(key for key in [*saved, *available] if isinstance(key, str) and key in available))


def _dateAlias(key):
	return key.replace("publicationDate:", "lastUpdated:", 1) if isinstance(key, str) \
		and key in ("publicationDate:asc", "publicationDate:desc") else key


def tabOrder():
	return orderedKeys(_setting("tabOrder", []), TABS)


def tabDefaults():
	text = _setting("tabDefaults", "{}")
	try:
		data = json.loads(text) if isinstance(text, str) and len(text) <= 65536 else {}
		return {key: {**value, "sort": _dateAlias(value["sort"])} if "sort" in value else value
			for key, value in data.items() if key in TABS and isinstance(value, dict)}
	except (ValueError, AttributeError, TypeError):
		return {}


def favouriteIds():
	values = _setting("favourites", [])
	if not isinstance(values, (list, tuple)):
		return set()
	return {value.strip().casefold() for value in values[:4096]
		if isinstance(value, str) and value.strip() and len(value) <= 256}


def _addonId(item):
	return str(getattr(item.model, "addonId", "")).strip().casefold()


def _sortKey(field, reverse=False):
	# Dev9 replaces the former first-publication presentation with the existing
	# Last updated sort. Preserve old semantic preferences through this alias.
	if field == "publicationDate":
		field = "lastUpdated"
	return "%s:%s" % (field, "desc" if reverse else "asc")


def sortEntries(vm):
	fields = list(getattr(vm, "sortableFields", vm.presentedFields))
	entries = []
	labels = list(vm._columnSortChoices)
	for index, field in enumerate(fields):
		if field.name == "publicationDate":
			continue
		for reverse in (False, True):
			logical = index * 2 + int(reverse)
			if logical < len(labels):
				entries.append((_sortKey(field.name, reverse), labels[logical], logical))
	for reverse in (False, True):
		logical = len(fields) * 2 + int(reverse)
		if logical < len(labels):
			entries.append((_sortKey("lastUpdated", reverse), labels[logical], logical))
	byKey = {entry[0]: entry for entry in entries}
	return [byKey[key] for key in orderedKeys(_setting("sortOrder", []), list(byKey))]


def activeSort(vm):
	date = getattr(vm, "_serrebiDateSort", None)
	return _sortKey("lastUpdated", date) if type(date) is bool else _sortKey(
		vm._sortByModelField.name, bool(vm._reverseSort),
	)


def contextualLayout(layout, vm):
	"""Name first, then every active sort value, including normally hidden relevance."""
	if _setting("contextualColumns", False) is not True:
		return layout
	fields = list(getattr(vm, "sortableFields", vm.presentedFields))
	byName = {field.name: field for field in fields}
	name = byName.get("displayName")
	active = activeSort(vm).split(":", 1)[0]
	field = byName.get("publicationDate", "lastUpdated") if active == "lastUpdated" else byName.get(active)
	first = [item for item in (name, field) if item is not None]
	result = []
	seen = set()
	for item in [*first, *layout]:
		key = item if isinstance(item, str) else item.name
		if key not in seen:
			result.append(item)
			seen.add(key)
	return result


class SortOrderDialog(wx.Dialog):
	def __init__(self, parent, keys, labels, order):
		super().__init__(parent, title=_("Arrange sort choices"))
		from gui import guiHelper
		self.keys = orderedKeys(order, keys)
		self.labels = labels
		sizer = wx.BoxSizer(wx.VERTICAL)
		helper = guiHelper.BoxSizerHelper(self, sizer=sizer)
		self.list = helper.addLabeledControl(_("Sort choices in display &order:"), wx.ListBox,
			choices=[labels[key] for key in self.keys])
		self.list.SetMinSize((440, 240))
		self.list.SetSelection(0)
		dialogRef = weakref.ref(self)
		for delta, label in ((-1, _("Move sort choice &up")), (1, _("Move sort choice &down"))):
			button = helper.addItem(wx.Button(self, label=label))
			button.Bind(wx.EVT_BUTTON, lambda event, delta=delta: (
				current.move(delta) if (current := dialogRef()) is not None else event.Skip()
			))
		helper.addItem(self.CreateButtonSizer(wx.OK | wx.CANCEL))
		self.SetSizerAndFit(sizer)


	def move(self, delta):
		index = self.list.GetSelection()
		other = index + delta
		if index < 0 or not 0 <= other < len(self.keys):
			return
		self.keys[index], self.keys[other] = self.keys[other], self.keys[index]
		self.list.Set([self.labels[key] for key in self.keys])
		self.list.SetSelection(other)
		import ui
		ui.message(_("{item}, position {position} of {total}").format(
			item=self.labels[self.keys[other]], position=other + 1, total=len(self.keys),
		))


class PreferencesDialog(wx.Dialog):
	def __init__(self, parent, feature, draft):
		super().__init__(parent, title=_("Store tabs and defaults"))
		from gui import guiHelper
		self.feature = feature
		self.order = orderedKeys(draft["tabOrder"], TABS)
		self.defaults = {key: dict(value) for key, value in draft["tabDefaults"].items()}
		self.sortOrder = list(draft["sortOrder"])
		self.current = None
		dialogRef = weakref.ref(self)
		sizer = wx.BoxSizer(wx.VERTICAL)
		helper = guiHelper.BoxSizerHelper(self, sizer=sizer)
		self.tabs = helper.addLabeledControl(_("&Tabs in display order:"), wx.ListBox,
			choices=[feature.tabLabel(key) for key in self.order])
		self.tabs.SetMinSize((440, 160))
		for delta, label in ((-1, _("Move tab &up")), (1, _("Move tab &down"))):
			button = helper.addItem(wx.Button(self, label=label))
			button.Bind(wx.EVT_BUTTON, lambda event, delta=delta: (
				current.move(delta) if (current := dialogRef()) is not None else event.Skip()
			))
		self.sort = helper.addLabeledControl(_("Default &sort for this tab:"), wx.Choice, choices=[])
		self.channel = helper.addLabeledControl(_("Default &channel for this tab:"), wx.Choice, choices=[])
		button = helper.addItem(wx.Button(self, label=_("Arrange sort &choices...")))
		button.Bind(wx.EVT_BUTTON, lambda event: (
			current.arrangeSort(event) if (current := dialogRef()) is not None else event.Skip()
		))
		self.tabs.Bind(wx.EVT_LISTBOX, lambda event: (
			current.selectTab(event) if (current := dialogRef()) is not None else event.Skip()
		))
		self.tabs.SetSelection(0)
		self.selectTab()
		helper.addItem(self.CreateButtonSizer(wx.OK | wx.CANCEL))
		self.SetSizerAndFit(sizer)


	def saveTab(self):
		if self.current is None:
			return
		self.defaults[self.current] = {
			"sort": self.sortKeys[self.sort.GetSelection()],
			"channel": self.channelKeys[self.channel.GetSelection()],
		}


	def selectTab(self, event=None):
		self.saveTab()
		self.current = self.order[self.tabs.GetSelection()]
		entries = self.feature.settingSortEntries(self.current)
		self.sortKeys = ["native"] + [key for key, label in entries]
		self.sort.Set([_("NVDA default")] + [label for key, label in entries])
		channels = list(self.feature.channels._channelFilters)
		if self.current in ("AVAILABLE", "UPDATE"):
			channels = [channel for channel in channels if channel.name != "EXTERNAL"]
		self.channelKeys = ["native"] + [channel.name for channel in channels]
		self.channel.Set([_("NVDA default")] + [channel.displayString for channel in channels])
		saved = self.defaults.get(self.current, {})
		self.sort.SetSelection(self.sortKeys.index(saved.get("sort")) if saved.get("sort") in self.sortKeys else 0)
		self.channel.SetSelection(self.channelKeys.index(saved.get("channel"))
			if saved.get("channel") in self.channelKeys else 0)


	def move(self, delta):
		index = self.tabs.GetSelection()
		other = index + delta
		if index < 0 or not 0 <= other < len(self.order):
			return
		self.order[index], self.order[other] = self.order[other], self.order[index]
		self.tabs.Set([self.feature.tabLabel(key) for key in self.order])
		self.tabs.SetSelection(other)
		import ui
		ui.message(_("{item}, position {position} of {total}").format(
			item=self.feature.tabLabel(self.current), position=other + 1, total=len(self.order),
		))


	def arrangeSort(self, event):
		entries = self.feature.settingSortEntries(None)
		labels = dict(entries)
		with SortOrderDialog(self, list(labels), labels, self.sortOrder) as dialog:
			if dialog.ShowModal() == wx.ID_OK:
				self.sortOrder = list(dialog.keys)


	def result(self):
		self.saveTab()
		return {"tabOrder": list(self.order), "tabDefaults": self.defaults, "sortOrder": self.sortOrder}


class _SelectionEvent:
	def __init__(self, selection, original=None):
		self.selection, self.original = selection, original

	def GetSelection(self):
		return self.selection

	def __getattr__(self, name):
		return getattr(self.original, name)


class CustomizationFeature:
	def __init__(self, plugin):
		self.plugin = plugin
		self.dialogs = weakref.WeakSet()
		self.active = True


	def tabLabel(self, key):
		return _("Favourites") if key == FAVOURITES else self.status._StatusFilterKey[key].displayString


	def settingSortEntries(self, tab):
		native = self.status._StatusFilterKey["AVAILABLE" if tab == FAVOURITES else tab] if tab else None
		fields = [field for field in self.lists.AddonListField
			if field.name != "publicationDate" and (
				(field.name == "searchRank" and hasattr(self.lists.AddonListVM, "sortableFields"))
				or field.name != "searchRank" and (native is None or native not in field.hideStatuses))]
		entries = [(_sortKey(field.name, reverse), _("{column} ({direction})").format(
			column=field.displayString, direction=_("descending") if reverse else _("ascending"),
		)) for field in fields for reverse in (False, True)]
		entries += [(_sortKey("lastUpdated", reverse), _("Last updated ({direction})").format(
			direction=_("descending") if reverse else _("ascending"),
		)) for reverse in (False, True)]
		return entries


	def logicalTab(self, dialog):
		names = getattr(dialog, "_serrebiTabNames", None)
		index = dialog.addonListTabs.GetSelection() if hasattr(dialog, "addonListTabs") else -1
		return names[index] if names and 0 <= index < len(names) else dialog._storeVM._filteredStatusKey.name


	def syncSort(self, dialog):
		entries = sortEntries(dialog._storeVM.listVM)
		dialog._serrebiSortEntries = entries
		dialog.columnFilterCtrl.Set([entry[1] for entry in entries])
		key = activeSort(dialog._storeVM.listVM)
		keys = [entry[0] for entry in entries]
		if key in keys:
			dialog.columnFilterCtrl.SetSelection(keys.index(key))


	def applyDefaults(self, dialog):
		key = self.logicalTab(dialog)
		defaults = tabDefaults().get(key, {})
		store = dialog._storeVM
		vm = store.listVM
		channelName = defaults.get("channel", "native")
		if key == FAVOURITES and channelName == "native":
			channelName = "ALL"
		for index, channel in enumerate(self.channels._channelFilters):
			if channel.name == channelName and index < dialog.channelFilterCtrl.GetCount():
				store._filterChannelKey = channel
				dialog.channelFilterCtrl.SetSelection(index)
		key = defaults.get("sort", "native")
		entries = sortEntries(vm)
		if key in [entry[0] for entry in entries]:
			fieldName, direction = key.split(":")
			if fieldName == "lastUpdated":
				vm._serrebiDateSort = direction == "desc"
			else:
				fields = list(getattr(vm, "sortableFields", vm.presentedFields))
				field = next(field for field in fields if field.name == fieldName)
				vm.setSortField(field, direction == "desc")
		self.syncSort(dialog)
		dialog._setListLabels()


	def refreshColumns(self, dialog):
		if hasattr(dialog, "addonListView"):
			dialog.addonListView._refreshColumns()


	def applySort(self, dialog, key, event=None):
		entry = next((entry for entry in sortEntries(dialog._storeVM.listVM) if entry[0] == key), None)
		if entry is None:
			return
		dialog._serrebiPendingSort = None
		self.originalColumnChange(dialog, _SelectionEvent(entry[2], event))
		self.syncSort(dialog)
		self.refreshColumns(dialog)


	def stageSort(self, dialog, key, event=None):
		if _setting("sortOnEnter", False) is True:
			dialog._serrebiPendingSort = key
			keys = [entry[0] for entry in dialog._serrebiSortEntries]
			if key in keys:
				dialog.columnFilterCtrl.SetSelection(keys.index(key))
			if event is not None and hasattr(event, "GetColumn"):
				dialog.columnFilterCtrl.SetFocus()
		else:
			self.applySort(dialog, key, event)


	def rebuildTabs(self, dialog):
		book = dialog.addonListTabs
		if not book.GetPageCount():
			return
		oldIndex = book.GetSelection()
		native = list(self.status._statusFilters)
		wanted = getattr(dialog, "_serrebiInitialTab", None)
		if wanted is None:
			wanted = native[max(oldIndex, 0)].name
		page = book.GetPage(0)
		book.Unbind(wx.EVT_NOTEBOOK_PAGE_CHANGED)
		dialog._serrebiBuildingTabs = True
		book.Freeze()
		try:
			while book.GetPageCount():
				if not book.RemovePage(book.GetPageCount() - 1):
					raise RuntimeError("Cannot reorder Add-on Store tabs")
			dialog._serrebiTabNames = tabOrder()
			for key in dialog._serrebiTabNames:
				book.AddPage(page, self.tabLabel(key))
			book.ChangeSelection(dialog._serrebiTabNames.index(wanted))
		finally:
			book.Thaw()
			dialog._serrebiBuildingTabs = False
			dialogRef = weakref.ref(dialog)
			def onPageChanged(event):
				current = dialogRef()
				if current is not None:
					return current.onListTabPageChange(event)
				if event is not None:
					event.Skip()
			book.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, onPageChanged, book)


	def keyPressed(self, dialog, event):
		key = event.GetKeyCode()
		if key in (ord("C"), ord("c")) and event.ControlDown() and not event.AltDown() and not event.ShiftDown():
			focus = wx.Window.FindFocus()
			if focus is dialog.addonListView:
				selected = dialog._storeVM.listVM.getSelection()
				for action in getattr(dialog._storeVM, "actionVMList", ()):
					if getattr(action, "_serrebiShareAction", False) and selected is not None:
						action.actionTarget = selected
						if action.isValid:
							action.actionHandler(selected)
							return
		if event.ControlDown() and not event.AltDown() and not event.ShiftDown() and ord("1") <= key <= ord("9"):
			index = key - ord("1")
			if index < len(dialog._serrebiTabNames):
				if index != dialog.addonListTabs.GetSelection():
					dialog.addonListTabs.ChangeSelection(index)
					dialog.onListTabPageChange(None)
				dialog.addonListView.SetFocus()
				return
		if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and wx.Window.FindFocus() is dialog.columnFilterCtrl:
			pending = getattr(dialog, "_serrebiPendingSort", None)
			if pending:
				self.applySort(dialog, pending, event)
				return
		event.Skip()


	def installedItem(self, store, item):
		if item is None:
			return None
		for channel in store._installedAddons.values():
			for model in channel.values():
				if str(model.addonId).casefold() == _addonId(item):
					return self.lists.AddonListItemVM(model=model,
						status=self.status.getStatus(model, self.status._StatusFilterKey.INSTALLED))
		return None


	def toggleFavourite(self, store, item, add):
		if not self.active or _secure() or item is None:
			return
		ids = favouriteIds()
		identifier = _addonId(item)
		if not identifier:
			return
		if add:
			if len(ids) >= 4096 and identifier not in ids:
				import ui
				ui.message(_("The favourites list is full."))
				return
			ids.add(identifier)
		else:
			ids.discard(identifier)
		config.conf["serrebiStore"]["favourites"] = sorted(ids)
		import ui
		ui.message(_("Added to favourites.") if add else _("Removed from favourites."))
		# Rebuild only existing native models; marking does not cause a network fetch.
		for dialog in list(self.dialogs):
			if dialog.IsBeingDeleted():
				continue
			if getattr(dialog._storeVM, "_serrebiFavourites", False):
				vm = dialog._storeVM
				vm.listVM.resetListItems(vm._createListItemVMs())
				vm.detailsVM.listItem = vm.listVM.getSelection()
			else:
				for action in dialog._storeVM.actionVMList:
					if hasattr(action, "_notify"):
						action._notify()


	def favouriteRows(self, store, nativeRows):
		ids = favouriteIds()
		rows = [item for item in nativeRows if _addonId(item) in ids]
		catalogIds = {_addonId(item) for item in rows}
		for channel, models in store._installedAddons.items():
			if channel not in self.channels._channelFilters[store._filterChannelKey]:
				continue
			for model in models.values():
				identifier = str(model.addonId).casefold()
				if identifier not in ids or identifier in catalogIds or model.legacy:
					continue
				if not store._filterByEnabledKey(model):
					continue
				rows.append(self.lists.AddonListItemVM(model=model,
					status=self.status.getStatus(model, self.status._StatusFilterKey.AVAILABLE)))
		return rows


	def enable(self, settingsPanel):
		if _secure():
			return
		config.conf.spec["serrebiStore"].update(_SPEC)
		self.status = importlib.import_module("addonStore.models.status")
		self.channels = importlib.import_module("addonStore.models.channel")
		self.lists = importlib.import_module("gui.addonStoreGui.viewModels.addonList")
		stores = importlib.import_module("gui.addonStoreGui.viewModels.store")
		dialogs = importlib.import_module("gui.addonStoreGui.controls.storeDialog")
		controls = importlib.import_module("gui.addonStoreGui.controls.addonList")
		actions = importlib.import_module("gui.addonStoreGui.viewModels.action")
		menus = importlib.import_module("gui.addonStoreGui.controls.actions")
		dialogClass = dialogs.AddonStoreDialog
		feature = self
		originalInit = dialogClass.__init__
		originalControls = dialogClass._createFilterControls
		originalTab = dialogClass.onListTabPageChange
		originalStatus = dialogClass._statusFilterKey
		originalTitle = dialogClass._titleText
		originalLabel = dialogClass._listLabelText
		originalToggle = dialogClass._toggleFilterControls
		originalSearch = dialogClass.onFilterTextChange
		self.originalColumnChange = dialogClass.onColumnFilterChange
		originalColumnClick = controls.AddonVirtualList.OnColClick
		originalRows = stores.AddonStoreVM._createListItemVMs
		originalActions = stores.AddonStoreVM._makeActionsList

		def init(dialog, *args, **kwargs):
			dialogRef = weakref.ref(dialog)
			def resolveInitial(name):
				name = name if name in TABS else tabOrder()[0]
				dialog = dialogRef()
				if dialog is not None:
					dialog._serrebiInitialTab = name
				return feature.status._StatusFilterKey.AVAILABLE if name == FAVOURITES \
					else feature.status._StatusFilterKey[name]
			def callback(method, *arguments):
				dialog = dialogRef()
				if feature.active and dialog is not None:
					return method(dialog, *arguments)
			dialog._serrebiResolveInitialTab = resolveInitial
			dialog._serrebiLogicalTab = lambda: callback(feature.logicalTab)
			dialog._serrebiApplyTabDefaults = lambda: callback(feature.applyDefaults)
			dialog._serrebiSyncSortChoice = lambda: callback(feature.syncSort)
			def layout(columns):
				dialog = dialogRef()
				return contextualLayout(columns, dialog._storeVM.listVM) if feature.active and dialog else columns
			dialog._serrebiContextualLayout = layout
			def cleanup(event):
				current = dialogRef()
				if (current is not None and event is not None
						and event.GetEventObject() is current):
					feature.dialogs.discard(current)
					current._serrebiPendingSort = None
				if event is not None:
					event.Skip()
			originalInit(dialog, *args, **kwargs)
			feature.dialogs.add(dialog)
			dialog.Bind(wx.EVT_WINDOW_DESTROY, cleanup)
			def key(event):
				if feature.active and dialogRef() is not None:
					callback(feature.keyPressed, event)
				else:
					event.Skip()
			dialog.Bind(wx.EVT_CHAR_HOOK, key)

		def createControls(dialog, *args, **kwargs):
			dialog._serrebiBuildingControls = True
			try:
				feature.rebuildTabs(dialog)
				return originalControls(dialog, *args, **kwargs)
			finally:
				dialog._serrebiBuildingControls = False

		def statusKey(dialog):
			if not getattr(dialog, "_serrebiTabNames", None):
				return originalStatus.__get__(dialog, type(dialog))
			key = feature.logicalTab(dialog)
			return feature.status._StatusFilterKey.AVAILABLE if key == FAVOURITES \
				else feature.status._StatusFilterKey[key]

		def tabChange(dialog, event):
			if (getattr(dialog, "_serrebiBuildingTabs", False)
					or getattr(dialog, "_serrebiBuildingControls", False)):
				return
			dialog._serrebiPendingSort = None
			dialog._storeVM._serrebiFavourites = feature.logicalTab(dialog) == FAVOURITES
			result = originalTab(dialog, event)
			feature.syncSort(dialog)
			feature.refreshColumns(dialog)
			return result

		def toggle(dialog):
			originalToggle(dialog)
			if getattr(dialog._storeVM, "_serrebiFavourites", False):
				dialog.channelFilterCtrl.Append(feature.channels.Channel.EXTERNAL.displayString)
				dialog.enabledFilterCtrl.Show()
				dialog.enabledFilterCtrl.Enable()

		def title(dialog):
			if feature.logicalTab(dialog) == FAVOURITES:
				return "%s - %s (%s)" % (dialog.title, _("Favourites"), dialog._channelFilterKey.displayString)
			return originalTitle.__get__(dialog, type(dialog))

		def label(dialog):
			return _("Favourite &add-ons:") if feature.logicalTab(dialog) == FAVOURITES \
				else originalLabel.__get__(dialog, type(dialog))

		def columnChange(dialog, event):
			entries = getattr(dialog, "_serrebiSortEntries", sortEntries(dialog._storeVM.listVM))
			index = event.GetSelection()
			if 0 <= index < len(entries):
				feature.stageSort(dialog, entries[index][0], event)

		def columnClick(control, event):
			dialog = control.GetParent()
			if not getattr(dialog, "_serrebiSortEntries", None):
				return originalColumnClick(control, event)
			index = event.GetColumn()
			fields = getattr(control, "_serrebiColumnMap", [])
			if not 0 <= index < len(fields) or fields[index] == "source":
				return
			field = fields[index]
			name = field if isinstance(field, str) else field.name
			current = activeSort(dialog._storeVM.listVM)
			reverse = current == _sortKey(name, False)
			feature.stageSort(dialog, _sortKey(name, reverse), event)

		def search(dialog, event):
			before = activeSort(dialog._storeVM.listVM)
			result = originalSearch(dialog, event)
			if before != activeSort(dialog._storeVM.listVM):
				feature.syncSort(dialog)
				feature.refreshColumns(dialog)
			return result

		def rows(store):
			result = originalRows(store)
			return feature.favouriteRows(store, result) if getattr(store, "_serrebiFavourites", False) else result

		def canHelp(store, item):
			installed = feature.installedItem(store, item)
			return bool(installed and installed.model._addonHandlerModel is not None
				and installed.model._addonHandlerModel.getDocFilePath() is not None)

		def canRemove(store, item):
			installed = feature.installedItem(store, item)
			return bool(installed and installed.canUseRemoveAction())

		def makeActions(store):
			result = originalActions(store)
			selected = store.listVM.getSelection()
			for add, text in ((True, _("Add to &favourites")), (False, _("Remove from &favourites"))):
				result.append(actions.AddonActionVM(displayName=text,
					actionHandler=lambda item, add=add: feature.toggleFavourite(store, item, add),
					validCheck=lambda item, add=add: not _secure() and bool(_addonId(item))
						and ((_addonId(item) not in favouriteIds()) if add else (_addonId(item) in favouriteIds())),
					actionTarget=selected))
			for text, handler, valid in (
				(_("Help for installed &version"), store.helpAddon, canHelp),
				(_("Remove &installed version"), store.removeAddon, canRemove),
			):
				result.append(actions.AddonActionVM(displayName=text,
					actionHandler=lambda item, handler=handler: handler(feature.installedItem(store, item)),
					validCheck=lambda item, valid=valid: not _secure()
						and getattr(store, "_serrebiFavourites", False) and valid(store, item), actionTarget=selected))
			return result

		for owner, name, replacement in (
			(dialogClass, "__init__", init), (dialogClass, "_createFilterControls", createControls),
			(dialogClass, "_statusFilterKey", property(statusKey)),
			(dialogClass, "_titleText", property(title)), (dialogClass, "_listLabelText", property(label)),
			(dialogClass, "onListTabPageChange", tabChange), (dialogClass, "_toggleFilterControls", toggle),
			(dialogClass, "onColumnFilterChange", columnChange), (dialogClass, "onFilterTextChange", search),
			(controls.AddonVirtualList, "OnColClick", columnClick),
			(stores.AddonStoreVM, "_createListItemVMs", rows), (stores.AddonStoreVM, "_makeActionsList", makeActions),
		):
			self.plugin._rememberPatch(owner, name, replacement)
		self.patchMenus(menus, actions)
		self.patchSettings(settingsPanel)

	def patchMenus(self, menus, actions):
		feature = self
		originalChannel = menus._MonoActionsContextMenu._appendUpdateChannelSubMenu
		originalBatch = menus._BatchActionsContextMenu._actions

		def channel(menu):
			store = menu._storeVM
			if not getattr(store, "_serrebiFavourites", False):
				return originalChannel(menu)
			selected = store.listVM.getSelection()
			if selected is not None and feature.installedItem(store, selected) is not None:
				submenu = menus._UpdateChannelSubMenu(store)
				menu._contextMenu.AppendSubMenu(submenu._contextMenu, _("Upd&ate channel"))

		def batch(menu):
			result = originalBatch.__get__(menu, type(menu))
			store = menu._storeVM
			def installed(items):
				return [feature.installedItem(store, item) for item in items]
			def valid(items):
				rows = installed(items)
				return not _secure() and getattr(store, "_serrebiFavourites", False) \
					and bool(rows) and all(row is not None and row.canUseRemoveAction() for row in rows)
			result.append(actions.BatchAddonActionVM(
				displayName=_("Remove selected &installed versions"),
				actionHandler=lambda items: store.removeAddons(installed(items)),
				validCheck=valid, actionTarget=menu._selectedAddons,
			))
			return result

		self.plugin._rememberPatch(menus._MonoActionsContextMenu, "_appendUpdateChannelSubMenu", channel)
		self.plugin._rememberPatch(menus._BatchActionsContextMenu, "_actions", property(batch))

	def patchSettings(self, settingsPanel):
		feature = self
		originalSettings = settingsPanel.makeSettings
		originalSave = settingsPanel.onSave
		originalTerminate = self.plugin.terminate

		def settings(panel, sizer):
			originalSettings(panel, sizer)
			if _secure():
				return
			from gui import guiHelper
			helper = guiHelper.BoxSizerHelper(panel, sizer=sizer)
			panel._serrebiCustomizationDraft = {
				"tabOrder": tabOrder(), "sortOrder": _setting("sortOrder", []), "tabDefaults": tabDefaults(),
			}
			panel._serrebiCustomizationChecks = {}
			for key, label in (
				("sortOnEnter", _("Apply sorting only when &Enter is pressed")),
				("contextualColumns", _("Announce Name and the active sort &column first")),
			):
				check = helper.addItem(wx.CheckBox(panel, label=label))
				check.SetValue(bool(_setting(key, False)))
				panel._serrebiCustomizationChecks[key] = check
			button = helper.addItem(wx.Button(panel, label=_("Store &tabs and defaults...")))
			panelRef = weakref.ref(panel)
			def preferences(event):
				current = panelRef()
				if current is None:
					event.Skip()
					return
				with PreferencesDialog(current, feature, current._serrebiCustomizationDraft) as dialog:
					if dialog.ShowModal() == wx.ID_OK:
						current._serrebiCustomizationDraft = dialog.result()
			button.Bind(wx.EVT_BUTTON, preferences)

		def save(panel):
			originalSave(panel)
			if _secure() or not hasattr(panel, "_serrebiCustomizationDraft"):
				return
			section = config.conf["serrebiStore"]
			draft = panel._serrebiCustomizationDraft
			section["tabOrder"] = draft["tabOrder"]
			section["sortOrder"] = draft["sortOrder"]
			section["tabDefaults"] = json.dumps(draft["tabDefaults"], ensure_ascii=False)
			for key, check in panel._serrebiCustomizationChecks.items():
				section[key] = bool(check.GetValue())
			for dialog in list(feature.dialogs):
				try:
					if dialog.IsBeingDeleted():
						feature.dialogs.discard(dialog)
						continue
					feature.syncSort(dialog)
					feature.refreshColumns(dialog)
				except RuntimeError:
					# Settings may destroy Store before its panel callbacks run.
					feature.dialogs.discard(dialog)

		def terminate():
			feature.active = False
			return originalTerminate()

		self.plugin._rememberPatch(settingsPanel, "makeSettings", settings)
		self.plugin._rememberPatch(settingsPanel, "onSave", save)
		self.plugin._rememberPatch(self.plugin, "terminate", terminate)
