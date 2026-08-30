# SerrebiRadio NVDA Add-on Store Mirror helper.
# Points NVDA's built-in Add-on Store at the SerrebiRadio mirror.
# Adapted from nvdacn/NVDAUpdateMirror (GPL v2).

import config
import globalPluginHandler
import threading
from logHandler import log

MIRROR_STORE_URL = "https://serrebidev.github.io/nvda-addon-mirror"

confspec = {
	"originalStoreURL": "string(default='')",
}
config.conf.spec["serrebiStore"] = confspec
if "serrebiStore" not in config.conf:
	config.conf["serrebiStore"] = {}


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		currentURL = config.conf["addonStore"]["baseServerURL"]
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
		log.info(f"Set the Add-on store mirror to: {MIRROR_STORE_URL}")
		self._refreshStore()

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
		config.conf["addonStore"]["baseServerURL"] = self._originalURL
		log.info(f"Restored the Add-on store URL to: {self._originalURL}")
