"""Source-aware routing for NVDA's shared Add-on Store metadata manager."""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
from urllib.parse import urlparse

OFFICIAL = ""
MIRROR = "https://serrebidev.github.io/nvda-addon-mirror"
_source = contextvars.ContextVar("serrebiAddonStoreSource", default=None)


def validCustomURL(value):
	if not isinstance(value, str):
		return None
	value = value.strip()
	if any(char.isspace() or ord(char) < 32 for char in value):
		return None
	try:
		parsed = urlparse(value)
		port = parsed.port
	except ValueError:
		return None
	if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
		return None
	if parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535):
		return None
	return value.rstrip("/")

def selectedURL(settings):
	policy = settings.get("storePolicy", "mirror")
	if policy == "official":
		return OFFICIAL
	if policy == "original":
		return settings.get("originalStoreURL", "")
	if policy == "custom":
		return validCustomURL(settings.get("customStoreURL", "")) or MIRROR
	return MIRROR


class Router:
	"""Route and serialize core metadata calls without replacing its singleton."""
	def __init__(self, defaultURL, initialThread=None, initialURL=None, maxCaches=4):
		self.defaultURL, self.initialThread, self.initialURL = defaultURL, initialThread, initialURL
		self.maxCaches = maxCaches
		self.lock, self.activeLock = threading.RLock(), threading.RLock()
		self.caches, self.owned = {}, set()
		self.active = 0
		self.network = self.originalBaseURL = self.baseReplacement = self.hold = None
		self.officialBaseURL = None

	@contextlib.contextmanager
	def source(self, url):
		token = _source.set(url)
		try:
			yield
		finally:
			_source.reset(token)

	def currentURL(self):
		# The manager's startup worker existed before the router was installed.
		# It must keep its original source both for network routing and for cache
		# attribution.  Otherwise a dynamically patched startup fetch can store
		# the old catalog under the newly selected source.
		if self._isInitial():
			return self.initialURL or OFFICIAL
		return self.defaultURL if _source.get() is None else _source.get()

	def install(self, network, dataManager, storeModule, patch):
		self.network, self.originalBaseURL = network, network._getBaseURL
		self.officialBaseURL = network._DEFAULT_BASE_URL
		def getBaseURL():
			if self._isInitial():
				return self.initialURL or self.officialBaseURL
			return self.currentURL() or self.officialBaseURL
		self.baseReplacement = getBaseURL
		self._patch(patch, network, "_getBaseURL", self.baseReplacement)
		for name in ("getLatestCompatibleAddons", "getLatestAddons"):
			original = getattr(dataManager._DataManager, name)
			def fetch(manager, *args, _original=original, **kwargs):
				url = self.currentURL()
				self._begin()
				try:
					self._waitForInitial()
					with self.lock, self.source(url):
						self._activate(manager, url)
						result = _original(manager, *args, **kwargs)
						self._remember(manager, url)
						return result
				finally:
					self._end()
			self._patch(patch, dataManager._DataManager, name, fetch)
		for name in ("_cacheCompatibleAddons", "_cacheLatestAddons"):
			original = getattr(dataManager._DataManager, name)
			def cache(manager, *args, _original=original, _name=name, **kwargs):
				path = manager._cacheCompatibleFile \
					if _name.endswith("CompatibleAddons") else manager._cacheLatestFile
				before = self._fileStamp(path)
				result = _original(manager, *args, **kwargs)
				data = kwargs.get("addonData", args[0] if args else None)
				hashValue = kwargs.get("cacheHash", args[1] if len(args) > 1 else None)
				if data and hashValue and before != self._fileStamp(path) and not self._isInitial():
					self._markCacheFile(path)
				return result
			self._patch(patch, dataManager._DataManager, name, cache)
		originalCached = dataManager._DataManager._getCachedAddonData
		def getCached(manager, path, *args, **kwargs):
			try:
				with open(path, "r", encoding="utf-8") as file:
					if json.load(file).get("serrebiStoreSource") != self.currentURL():
						return None
			except (AttributeError, OSError, ValueError):
				return None
			return originalCached(manager, path, *args, **kwargs)
		self._patch(patch, dataManager._DataManager, "_getCachedAddonData", getCached)
		originalInit = storeModule.AddonStoreVM.__init__
		def init(vm, *args, **kwargs):
			vm._serrebiStoreURL = self.currentURL()
			return originalInit(vm, *args, **kwargs)
		self._patch(patch, storeModule.AddonStoreVM, "__init__", init)
		originalFetch = storeModule.AddonStoreVM._getAvailableAddonsInBG
		def fetchAvailable(vm, *args, **kwargs):
			with self.source(getattr(vm, "_serrebiStoreURL", self.defaultURL)):
				return originalFetch(vm, *args, **kwargs)
		self._patch(patch, storeModule.AddonStoreVM, "_getAvailableAddonsInBG", fetchAvailable)

	def _patch(self, patch, owner, name, replacement):
		patch(owner, name, replacement)
		self.owned.add(replacement)

	def _waitForInitial(self):
		initial = self.initialThread
		if initial is not None and initial is not threading.current_thread() and initial.is_alive():
			initial.join()

	def _isInitial(self):
		return self.initialThread is threading.current_thread()

	def _begin(self):
		with self.activeLock:
			self.active += 1

	def _end(self):
		with self.activeLock:
			self.active -= 1
			if self.active == 0 and self.network and self.network._getBaseURL is self.hold:
				self.network._getBaseURL = self.originalBaseURL

	def _activate(self, manager, url):
		state = self.caches.get(url)
		if state is None:
			state = (
				manager._getCachedAddonData(manager._cacheLatestFile),
				manager._getCachedAddonData(manager._cacheCompatibleFile),
			)
			if len(self.caches) >= self.maxCaches:
				self.caches.pop(next(iter(self.caches)))
			self.caches[url] = state
		manager._latestAddonCache, manager._compatibleAddonCache = state

	def _remember(self, manager, url):
		self.caches[url] = (manager._latestAddonCache, manager._compatibleAddonCache)

	def _fileStamp(self, path):
		try:
			return os.stat(path).st_mtime_ns
		except OSError:
			return None

	def _markCacheFile(self, path):
		try:
			with open(path, "r", encoding="utf-8") as file:
				data = json.load(file)
			data["serrebiStoreSource"] = self.currentURL()
			with open(path, "w", encoding="utf-8") as file:
				json.dump(data, file, ensure_ascii=False)
		except (AttributeError, OSError, ValueError):
			return

	def prepareRestore(self):
		with self.activeLock:
			if self.active == 0 or self.network is None:
				return
			if self.network._getBaseURL is not self.baseReplacement:
				return
			def hold():
				if self._isInitial():
					return self.initialURL or self.officialBaseURL
				url = _source.get()
				return self.originalBaseURL() if url is None else url or self.officialBaseURL
			self.hold = hold
			self.network._getBaseURL = hold
