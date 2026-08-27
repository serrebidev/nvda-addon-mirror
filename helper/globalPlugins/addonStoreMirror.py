# SerrebiRadio NVDA Add-on Store Mirror helper.
# Points NVDA's built-in Add-on Store at the SerrebiRadio mirror.
# Adapted from nvdacn/NVDAUpdateMirror (GPL v2).

import config
import globalPluginHandler
from logHandler import log

MIRROR_STORE_URL = "https://serrebidev.github.io/nvda-addon-mirror/"

confspec = {
	"originalStoreURL": "string(default='')",
}
config.conf.spec["serrebiStore"] = confspec
if "serrebiStore" not in config.conf:
	config.conf["serrebiStore"] = {}


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		self._originalURL = config.conf["addonStore"]["baseServerURL"]
		config.conf["serrebiStore"]["originalStoreURL"] = self._originalURL
		config.conf["addonStore"]["baseServerURL"] = MIRROR_STORE_URL
		log.info(f"Set the Add-on store mirror to: {MIRROR_STORE_URL}")
		self._refreshStore()

	def _refreshStore(self):
		try:
			from addonStore import dataManager

			dataManager.initialize()
		except Exception:
			log.exception("Failed to re-initialize the add-on store data manager")

	def terminate(self):
		original = config.conf["serrebiStore"]["originalStoreURL"]
		config.conf["addonStore"]["baseServerURL"] = original
		log.info(f"Restored the Add-on store URL to: {original}")
		self._refreshStore()
