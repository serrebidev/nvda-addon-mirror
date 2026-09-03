# SerrebiRadio NVDA Add-on Store Mirror helper.
# Points NVDA's built-in Add-on Store at the SerrebiRadio mirror and displays
# the winning upstream source for each catalog entry.
# Adapted from nvdacn/NVDAUpdateMirror (GPL v2).
#
# NVDA 2025.1 is the floor. Earlier releases hardcode
# addonStore.network.BASE_URL and have no [addonStore] baseServerURL setting,
# so no add-on can redirect their Add-on Store anywhere.

import importlib
import threading

import addonHandler
import config
import globalPluginHandler
from logHandler import log

addonHandler.initTranslation()

MIRROR_STORE_URL = "https://serrebidev.github.io/nvda-addon-mirror"
STORE_SOURCE_KEY = "storeSource"
MODEL_SOURCE_ATTRIBUTE = "_serrebiStoreSource"

confspec = {
	"originalStoreURL": "string(default='')",
}
config.conf.spec["serrebiStore"] = confspec
if "serrebiStore" not in config.conf:
	config.conf["serrebiStore"] = {}


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		self._sourceSupportPatches = []
		self._originalURL = ""
		self._urlApplied = False
		try:
			currentURL = config.conf["addonStore"]["baseServerURL"]
		except KeyError:
			# Only reachable when compatibility was overridden: the manifest
			# requires 2025.1. Report it and change nothing, rather than patch
			# the Add-on Store GUI of an NVDA that can never use the mirror.
			log.error(
				"This NVDA has no [addonStore] baseServerURL setting, so its "
				"Add-on Store cannot be pointed at a mirror. NVDA 2025.1 or "
				"later is required.",
			)
			return
		savedURL = config.conf["serrebiStore"]["originalStoreURL"]
		# NVDA persists baseServerURL. On the next startup it may already point at
		# this mirror, so do not overwrite the remembered official/custom URL with
		# the mirror itself. Older helper builds could already have done that;
		# NVDA's empty default means "use the official store" and is safe here.
		if currentURL != MIRROR_STORE_URL:
			self._originalURL = currentURL
			config.conf["serrebiStore"]["originalStoreURL"] = currentURL
		else:
			self._originalURL = "" if savedURL == MIRROR_STORE_URL else savedURL
		config.conf["addonStore"]["baseServerURL"] = MIRROR_STORE_URL
		self._urlApplied = True
		log.info(f"Set the Add-on store mirror to: {MIRROR_STORE_URL}")
		self._enableSourceSupport()
		self._refreshStore()

	def _rememberPatch(self, owner, name, replacement):
		"""Replace an attribute and remember enough state to restore it safely."""
		original = getattr(owner, name)
		setattr(owner, name, replacement)
		self._sourceSupportPatches.append((owner, name, original, replacement))

	def _enableSourceSupport(self):
		"""Preserve mirror provenance and add it to NVDA's Add-on Store list."""
		try:
			modelModule = importlib.import_module("addonStore.models.addon")
			listControlModule = importlib.import_module(
				"gui.addonStoreGui.controls.addonList"
			)
			listViewModelModule = importlib.import_module(
				"gui.addonStoreGui.viewModels.addonList"
			)

			for functionName in (
				"_createStoreModelFromData",
				"_createInstalledStoreModelFromData",
			):
				original = getattr(modelModule, functionName)

				def createModel(addonData, _original=original):
					model = _original(addonData)
					source = addonData.get(STORE_SOURCE_KEY)
					if isinstance(source, str) and source.strip():
						# Store models are frozen dataclasses, so normal assignment is
						# intentionally unavailable.
						object.__setattr__(model, MODEL_SOURCE_ATTRIBUTE, source.strip())
					return model

				self._rememberPatch(modelModule, functionName, createModel)

			modelBase = modelModule._AddonGUIModel
			originalAsDict = modelBase.asdict

			def asdict(model):
				data = originalAsDict(model)
				source = _getModelSource(model)
				if source:
					# Preserve provenance in NVDA's per-add-on cache so installed
					# and update entries can keep displaying their source.
					data[STORE_SOURCE_KEY] = source
				return data

			self._rememberPatch(modelBase, "asdict", asdict)

			listControl = listControlModule.AddonVirtualList
			originalRefreshColumns = listControl._refreshColumns

			def refreshColumns(control):
				originalRefreshColumns(control)
				control.InsertColumn(
					control.GetColumnCount(),
					# Translators: The add-on catalog or release source shown in the Add-on Store.
					_("Source"),
					width=control.scaleSize(140),
				)

			self._rememberPatch(listControl, "_refreshColumns", refreshColumns)

			originalGetItemText = listControl.OnGetItemText

			def getItemText(control, itemIndex, colIndex):
				if colIndex == len(control._addonsListVM.presentedFields):
					return _getSourceAtIndex(control._addonsListVM, itemIndex)
				return originalGetItemText(control, itemIndex, colIndex)

			self._rememberPatch(listControl, "OnGetItemText", getItemText)

			originalColClick = listControl.OnColClick

			def onColClick(control, event):
				# Source is informational. Ignore its header rather than passing an
				# out-of-range field index to NVDA's built-in sorting code.
				if event.GetColumn() == len(control._addonsListVM.presentedFields):
					return
				return originalColClick(control, event)

			self._rememberPatch(listControl, "OnColClick", onColClick)

			listItemViewModel = listViewModelModule.AddonListItemVM
			searchableText = getattr(listItemViewModel, "searchableText", None)
			if isinstance(searchableText, property):
				def getSearchableText(listItem):
					text = searchableText.__get__(listItem, type(listItem))
					source = _getModelSource(listItem.model).casefold()
					return f"{text} {source}".strip()

				self._rememberPatch(
					listItemViewModel,
					"searchableText",
					property(getSearchableText, doc=searchableText.__doc__),
				)
			else:
				# NVDA 2025.1 through 2025.3 have no searchableText property and
				# filter inside _getFilteredSortedIds instead.
				listViewModel = listViewModelModule.AddonListVM
				originalFilteredIds = listViewModel._getFilteredSortedIds

				def getFilteredSortedIds(viewModel):
					filteredIds = originalFilteredIds(viewModel)
					term = viewModel._filterString
					if not term:
						return filteredIds
					sourceMatches = {
						item.Id
						for item in viewModel._addons.values()
						if term.casefold() in _getModelSource(item.model).casefold()
					}
					if not sourceMatches:
						return filteredIds
					savedFilter = viewModel._filterString
					try:
						viewModel._filterString = None
						allSortedIds = originalFilteredIds(viewModel)
					finally:
						viewModel._filterString = savedFilter
					included = set(filteredIds) | sourceMatches
					return [addonId for addonId in allSortedIds if addonId in included]

				self._rememberPatch(
					listViewModel,
					"_getFilteredSortedIds",
					getFilteredSortedIds,
				)
		except Exception:
			self._restoreSourceSupport()
			log.exception("Failed to add source information to the Add-on Store")
		else:
			log.info("Added source information to the Add-on Store")

	def _restoreSourceSupport(self):
		for owner, name, original, replacement in reversed(self._sourceSupportPatches):
			if getattr(owner, name, None) is replacement:
				setattr(owner, name, original)
		self._sourceSupportPatches.clear()

	def _refreshStore(self):
		"""Refresh through NVDA's existing manager without replacing its singleton."""
		try:
			from addonStore import dataManager

			manager = dataManager.addonDataManager
			if manager is None:
				return

			def refresh():
				# Core starts an initial fetch before global plugins load. Let that
				# finish, then fetch again using the newly configured mirror URL.
				initial = getattr(manager, "_initialiseAvailableAddonsThread", None)
				if initial is not None and initial.is_alive():
					initial.join()
				if dataManager.addonDataManager is manager:
					manager.getLatestCompatibleAddons()

			threading.Thread(
				target=refresh,
				name="refreshAddonStoreMirror",
				daemon=True,
			).start()
		except Exception:
			log.exception("Failed to refresh the add-on store data manager")

	def terminate(self):
		self._restoreSourceSupport()
		if not self._urlApplied:
			return
		config.conf["addonStore"]["baseServerURL"] = self._originalURL
		log.info(f"Restored the Add-on store URL to: {self._originalURL}")


def _getModelSource(model):
	source = getattr(model, MODEL_SOURCE_ATTRIBUTE, "")
	return source if isinstance(source, str) else ""


def _getSourceAtIndex(listViewModel, index):
	"""Return provenance for a row across both old and current NVDA list VMs."""
	try:
		getAddon = getattr(listViewModel, "getAddonAtIndex", None)
		if getAddon is not None:
			return _getModelSource(getAddon(index).model)
		addonId = listViewModel._addonsFilteredOrdered[index]
		return _getModelSource(listViewModel._addons[addonId].model)
	except (AssertionError, IndexError, KeyError):
		# A background refresh can replace the model between wx requesting a
		# virtual row and requesting its column text.
		return ""
