"""Changelog support for the Add-on Store.

This module deliberately owns only its small cache and its patches.  It never
asks the Add-on Store downloader for an add-on package: GitHub's release API is
used only after the catalog has supplied a verified GitHub repository URL.
"""
import builtins
from http.client import HTTPException
import json
import math
import re
import threading
import time
import weakref
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_ = getattr(builtins, "_", lambda text: text)


CHANGELOG_KEY = "changelog"
RELEASE_TIME_KEY = "releaseTime"
MODEL_CHANGELOG_ATTRIBUTE = "_serrebiChangelog"
MODEL_RELEASE_TIME_ATTRIBUTE = "_serrebiReleaseTime"
_GITHUB_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)/?$")
CACHE_SECONDS = 15 * 60
MAX_RELEASES = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CACHED_REPOSITORIES = 64
MAX_CONCURRENT_FETCHES = 4
MAX_PENDING_REPOSITORIES = 64


def githubRepository(sourceURL):
	"""Return ``owner/repository`` only for a safe, canonical GitHub page."""
	if not isinstance(sourceURL, str):
		return None
	match = _GITHUB_REPO.match(sourceURL.strip())
	if not match:
		return None
	return "%s/%s" % match.groups()


def releaseTime(model):
	"""A source release timestamp in milliseconds, or ``None`` when unknown."""
	value = getattr(model, MODEL_RELEASE_TIME_ATTRIBUTE, None)
	if type(value) in (int, float) and math.isfinite(value) and value > 0:
		return value
	value = getattr(model, "submissionTime", None)
	return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def sortKey(model, descending=False):
	"""Unknown release times stay last in either direction."""
	value = releaseTime(model)
	if value is None:
		return (1, 0)
	return (0, -value if descending else value)


def isSecureDesktop():
	"""Fail closed when NVDA is running in its secure-desktop mode."""
	try:
		import globalVars
		return bool(globalVars.appArgs.secure)
	except (ImportError, AttributeError):
		return True


class ReleaseHistory:
	"""Small in-memory, TTL-bound GitHub release-notes cache."""
	def __init__(self, fetch=None, now=time.time):
		self._fetch = fetch or self._fetchJSON
		self._now = now
		self._cache = {}
		self._lock = threading.Lock()
		self._inFlight = {}
		self._pending = []
		self._activeFetches = 0

	def get(self, repository):
		if not repository:
			return None, "notGitHub"
		with self._lock:
			cached = self._cache.get(repository)
			if cached and self._now() - cached[0] < CACHE_SECONDS:
				return cached[1], cached[2]
		completed = threading.Event()
		answer = []
		def done(result, error):
			answer[:] = [result, error]
			completed.set()
		self.getAsync(repository, done)
		completed.wait()
		return tuple(answer)

	def getAsync(self, repository, callback):
		"""Fetch a repository once and notify every request waiting for it.

		The bounded scheduler keeps the Store responsive when a user invokes several
		changelogs, while duplicate requests join their repository's existing job.
		"""
		if not repository:
			callback(None, "notGitHub")
			return
		immediate = None
		with self._lock:
			cached = self._cache.get(repository)
			if cached and self._now() - cached[0] < CACHE_SECONDS:
				immediate = cached[1], cached[2]
			else:
				waiters = self._inFlight.get(repository)
				if waiters is not None:
					waiters.append(callback)
					return
				if len(self._inFlight) >= MAX_PENDING_REPOSITORIES:
					immediate = [], "networkError"
				else:
					self._inFlight[repository] = [callback]
					self._pending.append(repository)
					self._startPendingLocked()
		if immediate is not None:
			callback(*immediate)

	def _startPendingLocked(self):
		while self._pending and self._activeFetches < MAX_CONCURRENT_FETCHES:
			repository = self._pending.pop(0)
			self._activeFetches += 1
			threading.Thread(
				target=self._fetchAndNotify, args=(repository,), name="addonStoreChangelog", daemon=True,
			).start()

	def _fetchAndNotify(self, repository):
		try:
			try:
				releases = self._fetch(repository)
				if not isinstance(releases, list):
					raise ValueError("GitHub returned invalid release data")
				result, error = releases[:MAX_RELEASES], None
			except HTTPError as e:
				result, error = [], "rateLimit" if e.code in (403, 429) else "networkError"
			except (HTTPException, URLError, ValueError, OSError):
				result, error = [], "networkError"
			with self._lock:
				previous = self._cache.get(repository)
				# A refresh failure must not replace usable cached notes.
				if error and previous and previous[2] is None:
					result, error = previous[1], previous[2]
				elif repository not in self._cache and len(self._cache) >= MAX_CACHED_REPOSITORIES:
					del self._cache[next(iter(self._cache))]
				self._cache[repository] = (self._now(), result, error)
				callbacks = self._inFlight.pop(repository)
		finally:
			with self._lock:
				self._activeFetches -= 1
				self._startPendingLocked()
		for callback in callbacks:
			callback(result, error)

	@staticmethod
	def _fetchJSON(repository):
		url = "https://api.github.com/repos/%s/releases?per_page=%d" % (repository, MAX_RELEASES)
		request = Request(
			url,
			headers={"Accept": "application/vnd.github+json", "User-Agent": "NVDA-addonStoreMirror"},
		)
		with urlopen(request, timeout=10) as response:
			body = response.read(MAX_RESPONSE_BYTES + 1)
			if len(body) > MAX_RESPONSE_BYTES:
				raise ValueError("Release metadata exceeds the response limit")
			return json.loads(body.decode("utf-8"))


def _catalogNote(model):
	note = getattr(model, MODEL_CHANGELOG_ATTRIBUTE, None)
	if not isinstance(note, str):
		# External/manually installed models do not pass through the store-data
		# factories, but NVDA exposes their manifest changelog natively.
		note = getattr(model, "changelog", None)
	if not isinstance(note, str):
		manifest = getattr(model, "manifest", None)
		if hasattr(manifest, "get"):
			note = manifest.get("changelog")
	return note.strip() if isinstance(note, str) else ""


def _fallback(model, reason):
	note = _catalogNote(model)
	if note:
		provenance = "catalog" if reason in ("missing", None) else "catalog; %s" % reason
		return [(getattr(model, "addonVersionName", "Latest"), note, provenance)]
	homepage = getattr(model, "homepage", None)
	if isinstance(homepage, str) and homepage.startswith(("https://", "http://")):
		# Translators: No changelog was found; this is the catalog's author page.
		return [("", _("No release notes are available. Author page: {url}").format(url=homepage), reason)]
	# Translators: The catalog and release-history service have no changelog for this add-on.
	return [("", _("No release notes are available for this add-on."), reason)]


def _provenanceLabel(source):
	# Translators: Origins and fallback reasons for changelog records.
	labels = {
		"catalog": _("Catalog notes"), "GitHub release": _("GitHub release"),
		"notGitHub": _("Release history unavailable for this source"),
		"rateLimit": _("Release service rate limit reached"),
		"networkError": _("Release service unavailable"), "missing": _("No release notes published"),
	}
	if isinstance(source, str) and source.startswith("catalog; "):
		return labels["catalog"] + "; " + labels.get(source.split("; ", 1)[1], _("History unavailable"))
	return labels.get(source, _("History unavailable"))


def historyForModel(model, history):
	"""Return version, notes, provenance records without inventing history."""
	repository = githubRepository(getattr(model, "sourceURL", None))
	releases, error = history.get(repository)
	return _historyRows(model, releases, error)


def _historyRows(model, releases, error):
	"""Format fetched release data, including the catalog fallback."""
	if error:
		return _fallback(model, error)
	rows = []
	if _catalogNote(model):
		rows.append((getattr(model, "addonVersionName", "Latest"), _catalogNote(model), "catalog"))
	for release in releases:
		if not isinstance(release, dict):
			continue
		body = release.get("body")
		if isinstance(body, str) and body.strip():
			version = str(release.get("tag_name") or release.get("name") or _("Unknown version"))
			rows.append((version, body.strip(), "GitHub release"))
	return rows or _fallback(model, "missing")


class ChangelogFeature:
	def __init__(self, plugin):
		self.plugin = plugin
		self.history = ReleaseHistory()
		self._generation = 0
		self._requestGeneration = 0
		self._dialogs = weakref.WeakKeyDictionary()

	def terminate(self):
		self._generation += 1
		self._requestGeneration += 1

	def enable(self):
		if isSecureDesktop():
			return
		import importlib
		modelModule = importlib.import_module("addonStore.models.addon")
		storeModule = importlib.import_module("gui.addonStoreGui.viewModels.store")
		dataManager = None
		try:
			dataManager = importlib.import_module("addonStore.dataManager")
		except ImportError:
			pass
		for name in ("_createStoreModelFromData", "_createInstalledStoreModelFromData"):
			original = getattr(modelModule, name)
			def factory(data, _original=original):
				model = _original(data)
				for key, attribute in (
					(CHANGELOG_KEY, MODEL_CHANGELOG_ATTRIBUTE),
					(RELEASE_TIME_KEY, MODEL_RELEASE_TIME_ATTRIBUTE),
				):
					value = data.get(key)
					if isinstance(value, (str, int, float)):
						object.__setattr__(model, attribute, value)
				return model
			self.plugin._rememberPatch(modelModule, name, factory)
			if dataManager is not None and getattr(dataManager, name, None) is original:
				self.plugin._rememberPatch(dataManager, name, factory)
		base = modelModule._AddonGUIModel
		originalAsdict = base.asdict
		def asdict(model):
			data = originalAsdict(model)
			for key, attribute in (
				(CHANGELOG_KEY, MODEL_CHANGELOG_ATTRIBUTE),
				(RELEASE_TIME_KEY, MODEL_RELEASE_TIME_ATTRIBUTE),
			):
				value = getattr(model, attribute, None)
				if isinstance(value, (str, int, float)):
					data[key] = value
			return data
		self.plugin._rememberPatch(base, "asdict", asdict)
		self._patchSort(importlib)
		self._patchSortControl(importlib)
		self._patchDialogLifecycle(importlib)
		self._patchAction(storeModule)

	def _patchSort(self, importlib):
		listModule = importlib.import_module("gui.addonStoreGui.viewModels.addonList")
		cls = listModule.AddonListVM
		choices = getattr(cls, "_columnSortChoices", None)
		if not isinstance(choices, property):
			raise RuntimeError("Add-on Store sort choices are not patchable")
		original = cls._getFilteredSortedIds
		def filtered(vm):
			if not hasattr(vm, "_serrebiDateSort") or vm._serrebiDateSort is None:
				return original(vm)
			# Preserve core filtering, then replace only ordering. This also keeps
			# unknown dates last for both ascending and descending order.
			items = [vm._addons[addonId] for addonId in original(vm)]
			return [
				item.Id
				for item in sorted(items, key=lambda item: sortKey(item.model, vm._serrebiDateSort))
			]
		self.plugin._rememberPatch(cls, "_getFilteredSortedIds", filtered)
		originalSetSort = cls.setSortField
		def setSortField(vm, *args, **kwargs):
			vm._serrebiDateSort = None
			return originalSetSort(vm, *args, **kwargs)
		self.plugin._rememberPatch(cls, "setSortField", setSortField)
		def getChoices(vm):
			result = list(choices.__get__(vm, type(vm)))
			# Translators: An Add-on Store sort option using source release timestamps.
			result.append(_("Last updated (ascending)"))
			# Translators: An Add-on Store sort option using source release timestamps.
			result.append(_("Last updated (descending)"))
			return result
		self.plugin._rememberPatch(cls, "_columnSortChoices", property(getChoices))

	def _patchSortControl(self, importlib):
		"""Dispatch the two appended sort choices without adding a fake enum."""
		try:
			dialogModule = importlib.import_module("gui.addonStoreGui.controls.storeDialog")
		except ImportError as e:
			raise RuntimeError("Add-on Store sort control is unavailable") from e
		cls = getattr(dialogModule, "AddonStoreDialog", None)
		if cls is None or not hasattr(cls, "onColumnFilterChange"):
			raise RuntimeError("Add-on Store sort control is not patchable")
		original = cls.onColumnFilterChange
		def onColumnFilterChange(dialog, event):
			vm = dialog._storeVM.listVM
			choiceCount = len(vm._columnSortChoices)
			selection = event.GetSelection()
			if selection < choiceCount - 2:
				return original(dialog, event)
			oldOrder = vm._addonsFilteredOrdered
			vm._serrebiDateSort = bool(selection % 2)
			vm._updateAddonListing()
			saveBrowsing = getattr(dialog, "_serrebiSaveBrowsing", None)
			if saveBrowsing is not None:
				saveBrowsing()
			if oldOrder != vm._addonsFilteredOrdered:
				try:
					import core
					core.callLater(delay=0, callable=vm.updated.notify)
				except ImportError:
					vm.updated.notify()
		self.plugin._rememberPatch(cls, "onColumnFilterChange", onColumnFilterChange)

	def _patchDialogLifecycle(self, importlib):
		import wx
		dialogModule = importlib.import_module("gui.addonStoreGui.controls.storeDialog")
		cls = getattr(dialogModule, "AddonStoreDialog", None)
		if cls is None or not hasattr(cls, "__init__"):
			raise RuntimeError("Add-on Store dialog lifecycle is not patchable")
		original = cls.__init__
		feature = self
		def init(dialog, *args, **kwargs):
			original(dialog, *args, **kwargs)
			vm = dialog._storeVM
			dialogRef = weakref.ref(dialog)
			dialog._serrebiChangelogAlive = True
			feature._dialogs[vm] = dialogRef
			def onDestroy(event):
				currentDialog = dialogRef()
				if currentDialog is not None and event.GetEventObject() is currentDialog:
					currentDialog._serrebiChangelogAlive = False
					feature._requestGeneration += 1
					current = feature._dialogs.get(vm)
					if current is not None and current() is currentDialog:
						del feature._dialogs[vm]
				event.Skip()
			dialog.Bind(wx.EVT_WINDOW_DESTROY, onDestroy)
		feature.plugin._rememberPatch(cls, "__init__", init)

	def _patchAction(self, storeModule):
		vmClass = storeModule.AddonStoreVM
		original = vmClass._makeActionsList
		feature = self
		def actions(vm):
			result = original(vm)
			try:
				from gui.addonStoreGui.viewModels.action import AddonActionVM
			except ImportError:
				return result
			# Translators: Add-on Store action that opens the selected add-on's release notes.
			result.append(AddonActionVM(
				displayName=_("&Changelog"),
				actionHandler=lambda item: feature.show(item, vm),
				validCheck=lambda item: item is not None and not isSecureDesktop(),
				actionTarget=vm.listVM.getSelection(),
			))
			return result
		self.plugin._rememberPatch(vmClass, "_makeActionsList", actions)

	def show(self, item, storeVM):
		if item is None or isSecureDesktop():
			return
		dialogRef = self._dialogs.get(storeVM)
		if dialogRef is None or dialogRef() is None:
			return
		import ui
		# Translators: Release history is being retrieved in the background.
		ui.message(_("Loading changelog."))
		generation = self._generation
		self._requestGeneration += 1
		requestGeneration = self._requestGeneration
		def fetched(releases, error):
			rows = _historyRows(item.model, releases, error)
			if generation != self._generation or requestGeneration != self._requestGeneration:
				return
			try:
				import wx
				wx.CallAfter(
					self._showDialog, item.model.displayName, rows,
					generation, requestGeneration, dialogRef,
				)
			except Exception:
				return
		self.history.getAsync(githubRepository(getattr(item.model, "sourceURL", None)), fetched)

	def _showDialog(self, name, rows, generation, requestGeneration, dialogRef):
		parent = dialogRef()
		if (
			generation != self._generation
			or requestGeneration != self._requestGeneration
			or parent is None
			or not getattr(parent, "_serrebiChangelogAlive", False)
			or isSecureDesktop()
		):
			return
		import wx
		# Translators: Title of an Add-on Store dialog. {name} is the add-on name.
		dialog = wx.Dialog(parent, title=_("Changelog: {name}").format(name=name))
		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label for the release-version choice in the changelog viewer.
		sizer.Add(wx.StaticText(dialog, label=_("&Version and source:")), 0, wx.ALL, 8)
		choice = wx.Choice(dialog, choices=[
			"%s (%s)" % (version or _("Notes"), _provenanceLabel(source))
			for version, _notes, source in rows
		])
		sizer.Add(choice, 0, wx.EXPAND | wx.ALL, 8)
		# Translators: Label for the read-only changelog text.
		sizer.Add(wx.StaticText(dialog, label=_("Release &notes:")), 0, wx.LEFT | wx.RIGHT, 8)
		text = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
		def choose(_evt=None):
			text.SetValue(rows[choice.GetSelection()][1])
		choice.Bind(wx.EVT_CHOICE, choose)
		choice.SetSelection(0)
		choose()
		sizer.Add(text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
		buttons = dialog.CreateButtonSizer(wx.OK)
		sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
		dialog.SetSizerAndFit(sizer)
		dialog.SetSize((650, 450))
		try:
			dialog.ShowModal()
		finally:
			dialog.Destroy()
