"""Changelog support for the Add-on Store.

This module deliberately owns only its small cache and its patches.  It never
asks the Add-on Store downloader for an add-on package: GitHub's release API is
used only after the catalog has supplied a verified GitHub repository URL.
"""
import builtins
import base64
import binascii
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
import re
import sys
import threading
import time
import weakref
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_ = getattr(builtins, "_", lambda text: text)


CHANGELOG_KEY = "changelog"
RELEASE_TIME_KEY = "releaseTime"
LAST_UPDATED_TIME_KEY = "lastUpdatedTime"
ORIGINAL_PUBLICATION_TIME_KEY = "originalPublicationTime"
ORIGINAL_PUBLICATION_SOURCE_KEY = "originalPublicationSource"
CHANGELOG_SOURCE_KEY = "changelogSource"
CHANGELOG_SOURCES = ("all", "catalog", "github", "repository", "commits")
MODEL_CHANGELOG_ATTRIBUTE = "_serrebiChangelog"
MODEL_RELEASE_TIME_ATTRIBUTE = "_serrebiReleaseTime"
MODEL_LAST_UPDATED_TIME_ATTRIBUTE = "_serrebiLastUpdatedTime"
MODEL_ORIGINAL_PUBLICATION_TIME_ATTRIBUTE = "_serrebiOriginalPublicationTime"
MODEL_ORIGINAL_PUBLICATION_SOURCE_ATTRIBUTE = "_serrebiOriginalPublicationSource"
_GITHUB_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)/?$")
CACHE_SECONDS = 60 * 60
NEGATIVE_CACHE_SECONDS = 60
RELEASES_PER_PAGE = 100
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_HISTORY_SECONDS = 45
MAX_REPOSITORY_CHANGELOG_BYTES = 4 * 1024 * 1024
REPOSITORY_CHANGELOG_PATHS = (
	"changes.md", "CHANGELOG.md", "changelog.md", "CHANGES.md",
	"changes.txt", "CHANGELOG.txt", "changelog.txt", "CHANGES.txt",
	"CHANGELOG.rst", "changes.rst", "docs/changelog.md", "doc/changes.md",
	"docs/changes.md", "doc/changelog.md", "doc/en/changes.md", "addon/doc/en/changes.md",
)
MAX_RETAINED_REPOSITORY_BYTES = 4 * 1024 * 1024
MAX_CACHED_BYTES = 16 * 1024 * 1024
MAX_CACHED_REPOSITORIES = 64
MAX_CONCURRENT_FETCHES = 1
MAX_PENDING_REPOSITORIES = 64
RATE_LIMIT_FALLBACK_SECONDS = 15 * 60
MAX_RATE_LIMIT_COOLDOWN_SECONDS = 24 * 60 * 60

class HistoryFetchError(Exception):
	"""An interrupted traversal, including notes from completed pages."""
	def __init__(self, releases, reason, nextPage=None):
		super().__init__(reason)
		self.releases = releases
		self.reason = reason
		self.nextPage = nextPage


class HistoryRows(list):
	"""Viewer records with a separate, accessible completeness status."""
	def __init__(self, rows, error=None):
		super().__init__(rows)
		self.error = error
		self.omitted = 0
		self.usedCached = False
		self.hasGitHub = False
		self.dates = [None] * len(self)
		self.sourcePreference = "all"
		self.recordKind = "releases"
		self.nextPage = None
		self.withNotes = 0
		self.withoutNotes = 0
		self.retrievedCount = 0
		self.documentCount = 0


class ReleaseRecords(list):
	"""Compact fetched records, including whether cached records were retained."""
	usedCached = False
	limitReached = False
	nextPage = None
	recordKind = "releases"


def _recordBytes(record):
	# Count container/string memory conservatively, including shared keys each time.
	return sys.getsizeof(record) + sum(sys.getsizeof(key) + sys.getsizeof(value)
		for key, value in record.items())


def _recordsBytes(records):
	return sys.getsizeof(records) + sum(_recordBytes(record) for record in records)


def _boundedRecords(records):
	result = ReleaseRecords()
	result.cachedIdentities = set()
	cachedIdentities = getattr(records, "cachedIdentities", set())
	result.limitReached = bool(getattr(records, "limitReached", False))
	result.nextPage = getattr(records, "nextPage", None)
	result.recordKind = getattr(records, "recordKind", "releases")
	weight = sys.getsizeof(result)
	for record in records:
		if not isinstance(record, dict):
			continue
		cost = _recordBytes(record) + 16  # List pointer plus conservative allocation slack.
		if weight + cost > MAX_RETAINED_REPOSITORY_BYTES:
			result.limitReached = True
			break
		result.append(record)
		if _releaseIdentity(record) in cachedIdentities:
			result.usedCached = True
			result.cachedIdentities.add(_releaseIdentity(record))
		weight += cost
	return result


def _releaseIdentity(release):
	if release.get("path"):
		return ("path", str(release["path"]))
	if release.get("sha"):
		return ("sha", str(release["sha"]))
	if release.get("id") is not None:
		return ("id", str(release["id"]))
	return ("version", _versionIdentity(str(release.get("tag_name") or release.get("name") or "")))


def _mergeCachedRecords(refreshed, cached):
	result = ReleaseRecords()
	result.cachedIdentities = set()
	result.recordKind = getattr(refreshed, "recordKind", getattr(cached, "recordKind", "releases"))
	result.nextPage = (getattr(refreshed, "nextPage", None)
		or getattr(cached, "nextPage", None))
	seen = set()
	for index, release in enumerate([*refreshed, *cached]):
		if not isinstance(release, dict):
			continue
		identity = _releaseIdentity(release)
		if identity not in seen:
			seen.add(identity)
			result.append(release)
			if index >= len(refreshed):
				result.usedCached = True
				result.cachedIdentities.add(identity)
	return result


def githubRepository(sourceURL):
	"""Return ``owner/repository`` only for a safe, canonical GitHub page."""
	if not isinstance(sourceURL, str):
		return None
	match = _GITHUB_REPO.match(sourceURL.strip())
	if not match:
		return None
	return "%s/%s" % match.groups()


def _timestamp(value):
	"""Return a valid millisecond timestamp without guessing its provenance."""
	return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def _normalizedSourceURL(value):
	if not isinstance(value, str):
		return ""
	return value.strip().rstrip("/").casefold()


def _provenFirstPublication(data):
	"""Accept source-backed metadata uniformly, without add-on-specific rules."""
	value = _timestamp(data.get(ORIGINAL_PUBLICATION_TIME_KEY))
	source = data.get(ORIGINAL_PUBLICATION_SOURCE_KEY)
	if value is not None and isinstance(source, str) and source.strip():
		return value, source.strip()
	return None, None


def _learnFirstPublication(model, releases):
	"""Use only an author's explicitly identified initial release, on user retrieval."""
	if publicationTime(model) is not None and publicationSource(model):
		return
	repository = githubRepository(getattr(model, "sourceURL", None))
	if not repository:
		return
	initial = re.compile(
		r"^\s*(?:[-*]\s+|#{1,6}\s+)?(?:initial release|first public release)[.!]?\s*$",
		re.IGNORECASE | re.MULTILINE,
	)
	candidates = []
	for release in releases or []:
		if not isinstance(release, dict):
			continue
		body, tag = release.get("body"), release.get("tag_name")
		stamp = _publicationTime(release.get("published_at"))
		if isinstance(body, str) and isinstance(tag, str) and tag and stamp and initial.search(body):
			candidates.append((stamp * 1000, tag))
	# Conflicting initial-release claims do not establish one trustworthy date.
	if len(candidates) != 1:
		return
	stamp, tag = candidates[0]
	object.__setattr__(model, MODEL_ORIGINAL_PUBLICATION_TIME_ATTRIBUTE, stamp)
	object.__setattr__(model, MODEL_ORIGINAL_PUBLICATION_SOURCE_ATTRIBUTE,
		"https://github.com/%s/releases/tag/%s" % (repository, quote(tag, safe="")))


def releaseTime(model):
	"""The catalog's explicit source-publication timestamp for history labels."""
	return _timestamp(getattr(model, MODEL_RELEASE_TIME_ATTRIBUTE, None))


def publicationTime(model):
	"""The independently evidenced first-ever add-on publication time."""
	value = getattr(model, MODEL_ORIGINAL_PUBLICATION_TIME_ATTRIBUTE, None)
	if value is None:
		value = getattr(model, ORIGINAL_PUBLICATION_TIME_KEY, None)
	return _timestamp(value)


def publicationSource(model):
	"""Evidence URL(s) for :func:`publicationTime`, when supplied."""
	value = getattr(model, MODEL_ORIGINAL_PUBLICATION_SOURCE_ATTRIBUTE, None)
	if value is None:
		value = getattr(model, ORIGINAL_PUBLICATION_SOURCE_KEY, None)
	return value.strip() if isinstance(value, str) and value.strip() else None


def updatedTime(model):
	"""The native current-version catalog timestamp, or an extension fallback."""
	value = getattr(model, "submissionTime", None)
	if value is None:
		value = getattr(model, MODEL_LAST_UPDATED_TIME_ATTRIBUTE, None)
	if value is None:
		value = getattr(model, LAST_UPDATED_TIME_KEY, None)
	return _timestamp(value)


def _dateSortKey(model, timestamp, descending=False, addonId=""):
	"""Known dates first, with a deterministic name/id tie-breaker in both orders."""
	name = getattr(model, "displayName", "")
	name = name.casefold() if isinstance(name, str) else ""
	identity = addonId or getattr(model, "addonId", "")
	identity = str(identity).casefold()
	value = timestamp(model)
	if value is None:
		return (1, 0, name, identity)
	return (0, -value if descending else value, name, identity)


def sortKey(model, descending=False, addonId=""):
	"""Last-updated ordering; unknown update times stay last in both directions."""
	return _dateSortKey(model, updatedTime, descending, addonId)


def publicationSortKey(model, descending=False, addonId=""):
	"""First-publication ordering; unknown dates stay last in both directions."""
	return _dateSortKey(model, publicationTime, descending, addonId)


def _formattedDate(value):
	value = _timestamp(value)
	if value is None:
		return None
	try:
		return datetime.fromtimestamp(value / 1000).strftime("%x")
	except (OSError, OverflowError, ValueError):
		return None


def _unknownSortText(value):
	"""Identify textual absence without treating legitimate numeric zero as missing."""
	if value is None:
		return True
	if not isinstance(value, str):
		return False
	value = value.strip().casefold()
	return not value or value in {"unknown", _("Unknown").strip().casefold()}


def isSecureDesktop():
	"""Fail closed when NVDA is running in its secure-desktop mode."""
	try:
		import globalVars
		return bool(globalVars.appArgs.secure)
	except (ImportError, AttributeError):
		return True


class ReleaseHistory:
	"""Small in-memory, TTL-bound GitHub releases and commits cache."""
	def __init__(self, fetch=None, now=time.time):
		self._customFetch = fetch
		self._fetch = fetch or self._fetchJSON
		self._now = now
		self._cache = {}
		self._cacheBytes = {}
		self._lock = threading.Lock()
		self._inFlight = {}
		self._pending = []
		self._activeFetches = 0
		self._cooldownUntil = 0

	def get(self, repository, source="github", startPage=1, bypassCache=False):
		if not repository:
			return None, "notGitHub"
		cacheKey = self._cacheKey(repository, source, startPage)
		with self._lock:
			cached = self._cache.pop(cacheKey, None)
			if cached:
				self._cache[cacheKey] = cached  # Least recently used entry is first.
			if not bypassCache and cached and self._cachedAnswerUsable(cached):
				return cached[1], cached[2]
		completed = threading.Event()
		answer = []
		def done(result, error):
			answer[:] = [result, error]
			completed.set()
		self.getAsync(repository, done, source, startPage, bypassCache)
		completed.wait()
		return tuple(answer)

	@staticmethod
	def _cacheKey(repository, source, startPage):
		# Preserve the original public cache shape for the common release request.
		return repository if source == "github" and startPage == 1 else (repository, source, startPage)

	def _cachedAnswerUsable(self, cached):
		age = self._now() - cached[0]
		_records, error = cached[1], cached[2]
		if error == "rateLimit" and self._now() >= self._cooldownUntil:
			return False
		if error and not _records:
			return age < NEGATIVE_CACHE_SECONDS
		return age < CACHE_SECONDS

	def _storeCacheLocked(self, cacheKey, result, error):
		weight = _recordsBytes(result)
		self._cache.pop(cacheKey, None)
		self._cacheBytes.pop(cacheKey, None)
		while self._cache and (len(self._cache) >= MAX_CACHED_REPOSITORIES
			or sum(self._cacheBytes.values()) + weight > MAX_CACHED_BYTES):
			oldest = next(iter(self._cache))
			del self._cache[oldest]
			del self._cacheBytes[oldest]
		self._cache[cacheKey] = (self._now(), result, error)
		self._cacheBytes[cacheKey] = weight

	def getAsync(self, repository, callback, source="github", startPage=1, bypassCache=False):
		"""Fetch a repository once and notify every request waiting for it.

		The bounded scheduler keeps the Store responsive when a user invokes several
		changelogs, while duplicate requests join their repository's existing job.
		"""
		if not repository:
			callback(None, "notGitHub")
			return
		if source not in ("github", "repository", "repositoryIndex", "commits") \
			or type(startPage) is not int or startPage < 1:
			callback([], "networkError")
			return
		cacheKey = self._cacheKey(repository, source, startPage)
		immediate = None
		with self._lock:
			cached = self._cache.pop(cacheKey, None)
			if cached:
				self._cache[cacheKey] = cached
			if not bypassCache and cached and self._cachedAnswerUsable(cached):
				immediate = cached[1], cached[2]
			elif self._now() < self._cooldownUntil:
				if cached and cached[1]:
					immediate = cached[1], "rateLimit"
				else:
					immediate = ReleaseRecords(), "rateLimit"
			else:
				waiters = self._inFlight.get(cacheKey)
				if waiters is not None:
					waiters.append(callback)
					return
				if len(self._inFlight) >= MAX_PENDING_REPOSITORIES:
					immediate = [], "networkError"
				else:
					self._inFlight[cacheKey] = [callback]
					self._pending.append((repository, source, startPage, cacheKey))
					self._startPendingLocked()
		if immediate is not None:
			callback(*immediate)

	def _startPendingLocked(self):
		while self._pending and self._activeFetches < MAX_CONCURRENT_FETCHES:
			repository, source, startPage, cacheKey = self._pending.pop(0)
			self._activeFetches += 1
			threading.Thread(
				target=self._fetchAndNotify, args=(repository, source, startPage, cacheKey),
				name="addonStoreChangelog", daemon=True,
			).start()

	def _fetchAndNotify(self, repository, source, startPage, cacheKey):
		try:
			try:
				if self._now() < self._cooldownUntil:
					raise HistoryFetchError(ReleaseRecords(), "rateLimit")
				releases = (self._fetch(repository) if self._customFetch is not None
					and source == "github" and startPage == 1
					else self._fetchRepositoryIndex(repository) if source == "repositoryIndex"
					else self._fetchRepositoryChangelogs(repository, startPage) if source == "repository"
					else self._fetchJSON(repository, source, startPage))
				if not isinstance(releases, list):
					raise ValueError("GitHub returned invalid release data")
				result, error = releases, None
			except HistoryFetchError as e:
				result = e.releases if isinstance(e.releases, ReleaseRecords) else ReleaseRecords(e.releases)
				error = e.reason
				result.nextPage = e.nextPage
			except HTTPError as e:
				self._noteRateLimit(e.headers, e.code)
				result, error = [], "rateLimit" if e.code in (403, 429) else "networkError"
			except (HTTPException, URLError, ValueError, OSError):
				result, error = [], "networkError"
			with self._lock:
				previous = self._cache.get(cacheKey)
				# A refresh failure must not replace usable cached notes.
				if error and previous and previous[1]:
					result = _mergeCachedRecords(result, previous[1])
				result = _boundedRecords(result)
				if result.limitReached and not error:
					error = "historyLimit"
				self._storeCacheLocked(cacheKey, result, error)
				callbacks = self._inFlight.pop(cacheKey)
		finally:
			with self._lock:
				self._activeFetches -= 1
				self._startPendingLocked()
		for callback in callbacks:
			callback(result, error)

	def _noteRateLimit(self, headers, status=None):
		"""Apply one shared cooldown from GitHub rate-limit response metadata."""
		remaining = headers.get("X-RateLimit-Remaining") if headers else None
		if status not in (403, 429) and str(remaining).strip() != "0":
			return
		now = self._now()
		deadlines = []
		retryAfter = headers.get("Retry-After") if headers else None
		try:
			delay = float(retryAfter)
			if not math.isfinite(delay) or delay <= 0:
				raise ValueError
			deadlines.append(now + delay)
		except (TypeError, ValueError):
			try:
				deadline = parsedate_to_datetime(retryAfter).timestamp()
				if not math.isfinite(deadline) or deadline <= now:
					raise ValueError
				deadlines.append(deadline)
			except (TypeError, ValueError, OverflowError, OSError):
				pass
		reset = headers.get("X-RateLimit-Reset") if headers else None
		try:
			resetDeadline = float(reset)
			if not math.isfinite(resetDeadline) or resetDeadline <= now:
				raise ValueError
			deadlines.append(resetDeadline)
		except (TypeError, ValueError):
			pass
		until = max(deadlines) if deadlines else now + RATE_LIMIT_FALLBACK_SECONDS
		until = min(max(until, now), now + MAX_RATE_LIMIT_COOLDOWN_SECONDS)
		with self._lock:
			self._cooldownUntil = max(self._cooldownUntil, until)

	def _fetchJSON(self, repository, source="github", startPage=1):
		# Request numbered pages on the canonical API host, never arbitrary Link URLs.
		releases = ReleaseRecords()
		releases.recordKind = "commits" if source == "commits" else "releases"
		seen = set()
		retainedBytes = sys.getsizeof(releases)
		page = startPage
		endpoint = "commits" if source == "commits" else "releases"
		url = "https://api.github.com/repos/%s/%s?per_page=%d&page=%d" % (
			repository, endpoint, RELEASES_PER_PAGE, page,
		)
		request = Request(
			url,
			headers={"Accept": "application/vnd.github+json", "User-Agent": "NVDA-addonStoreMirror"},
		)
		try:
			with urlopen(request, timeout=10) as response:
				self._noteRateLimit(response.headers)
				body = response.read(MAX_RESPONSE_BYTES + 1)
				if len(body) > MAX_RESPONSE_BYTES:
					raise HistoryFetchError(releases, "historyLimit", page)
				data = json.loads(body.decode("utf-8"))
				if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
					raise ValueError("GitHub returned invalid release data")
				more = 'rel="next"' in response.headers.get("Link", "")
		except HTTPError as e:
			self._noteRateLimit(e.headers, e.code)
			raise HistoryFetchError(
				releases, "rateLimit" if e.code in (403, 429) else "networkError", page,
			) from e
		except (HTTPException, URLError, UnicodeError, TypeError, ValueError, OSError) as e:
			raise HistoryFetchError(releases, "networkError", page) from e
		for release in data:
			# Page boundaries can move while an author publishes. Keep each release once,
			# and retain only notes metadata rather than large asset/author objects.
			identity = _releaseIdentity(release)
			if identity in seen or (source != "commits" and release.get("draft")):
				continue
			seen.add(identity)
			if source == "commits":
				commit = release.get("commit") if isinstance(release.get("commit"), dict) else {}
				committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
				author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
				record = {
					"sha": release.get("sha") if isinstance(release.get("sha"), str) else None,
					"message": commit.get("message") if isinstance(commit.get("message"), str) else None,
					"date": committer.get("date") if isinstance(committer.get("date"), str)
						else author.get("date") if isinstance(author.get("date"), str) else None,
				}
			else:
				record = {key: release.get(key) if isinstance(release.get(key), str) else None
					for key in ("tag_name", "name", "body", "published_at")}
				record["id"] = release.get("id") if type(release.get("id")) in (int, str) else None
			retainedBytes += _recordBytes(record) + 16
			if retainedBytes > MAX_RETAINED_REPOSITORY_BYTES:
				raise HistoryFetchError(releases, "historyLimit", page)
			releases.append(record)
		if more:
			releases.nextPage = page + 1
		return releases

	def _fetchRepositoryIndex(self, repository):
		"""Discover allowlisted, non-empty changelog blobs with one bounded tree request."""
		records = ReleaseRecords()
		records.recordKind = "repositoryIndex"
		url = "https://api.github.com/repos/%s/git/trees/HEAD?recursive=1" % repository
		request = Request(url, headers={
			"Accept": "application/vnd.github+json", "User-Agent": "NVDA-addonStoreMirror",
		})
		try:
			with urlopen(request, timeout=10) as response:
				self._noteRateLimit(response.headers)
				body = response.read(MAX_RESPONSE_BYTES + 1)
				if len(body) > MAX_RESPONSE_BYTES:
					raise HistoryFetchError(records, "historyLimit")
				data = json.loads(body.decode("utf-8"))
		except HTTPError as e:
			self._noteRateLimit(e.headers, e.code)
			raise HistoryFetchError(records, "rateLimit" if e.code in (403, 429) else "networkError") from e
		except (HTTPException, URLError, UnicodeError, TypeError, ValueError, OSError) as e:
			raise HistoryFetchError(records, "networkError") from e
		if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
			raise HistoryFetchError(records, "networkError")
		byPath = {}
		allowed = set(REPOSITORY_CHANGELOG_PATHS)
		for item in data["tree"]:
			if not isinstance(item, dict):
				continue
			path, size = item.get("path"), item.get("size")
			if isinstance(path, str) and path in allowed and item.get("type") == "blob" \
				and item.get("mode") in ("100644", "100755") \
				and type(size) is int and size > 0:
				byPath[path] = {"path": path, "size": size}
		for path in REPOSITORY_CHANGELOG_PATHS:
			if path in byPath:
				records.append(byPath[path])
		if data.get("truncated") is True:
			records.limitReached = True
		return records

	def _repositoryIndex(self, repository):
		cacheKey = self._cacheKey(repository, "repositoryIndex", 1)
		with self._lock:
			cached = self._cache.get(cacheKey)
			if cached and self._cachedAnswerUsable(cached):
				return cached[1], cached[2]
		index = self._fetchRepositoryIndex(repository)
		error = "historyLimit" if index.limitReached else None
		index = _boundedRecords(index)
		with self._lock:
			self._storeCacheLocked(cacheKey, index, error)
		return index, error

	def _fetchRepositoryChangelogs(self, repository, startPage=1):
		"""Fetch one indexed allowlisted changelog document per user action."""
		index, indexError = self._repositoryIndex(repository)
		if not index or startPage > len(index):
			if indexError:
				raise HistoryFetchError(ReleaseRecords(), indexError)
			return ReleaseRecords()
		path = index[startPage - 1].get("path")
		if path not in REPOSITORY_CHANGELOG_PATHS:
			raise HistoryFetchError(ReleaseRecords(), "networkError")
		if self._now() < self._cooldownUntil:
			raise HistoryFetchError(ReleaseRecords(), "rateLimit")
		records = ReleaseRecords()
		records.recordKind = "repository"
		url = "https://api.github.com/repos/%s/contents/%s" % (repository, path)
		request = Request(url, headers={
			"Accept": "application/vnd.github+json", "User-Agent": "NVDA-addonStoreMirror",
		})
		try:
			with urlopen(request, timeout=10) as response:
				self._noteRateLimit(response.headers)
				body = response.read(min(MAX_RESPONSE_BYTES, MAX_REPOSITORY_CHANGELOG_BYTES) + 1)
				if len(body) > min(MAX_RESPONSE_BYTES, MAX_REPOSITORY_CHANGELOG_BYTES):
					raise HistoryFetchError(records, "historyLimit")
				data = json.loads(body.decode("utf-8"))
		except HTTPError as e:
			self._noteRateLimit(e.headers, e.code)
			raise HistoryFetchError(records, "rateLimit" if e.code in (403, 429) else "networkError") from e
		except (HTTPException, URLError, UnicodeError, TypeError, ValueError, OSError) as e:
			raise HistoryFetchError(records, "networkError") from e
		if not isinstance(data, dict) or data.get("type") != "file" or data.get("path") != path \
			or data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
			raise HistoryFetchError(records, "networkError")
		try:
			decoded = base64.b64decode("".join(data["content"].split()), validate=True)
			text = decoded.decode("utf-8")
		except (binascii.Error, UnicodeError) as e:
			raise HistoryFetchError(records, "networkError") from e
		if decoded and text.strip():
			records.append({"path": path, "text": text})
		if startPage < len(index):
			records.nextPage = startPage + 1
		if indexError:
			records.limitReached = True
		return records


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


def _sourceHasItems(source, model, records):
	"""Return whether discovery found at least one truthful item for a source."""
	if source == "catalog":
		return bool(_catalogNote(model))
	if source == "repository":
		if getattr(records, "recordKind", None) == "repositoryIndex":
			return any(
				isinstance(record, dict)
				and record.get("path") in REPOSITORY_CHANGELOG_PATHS
				and type(record.get("size")) is int and record["size"] > 0
				for record in records or ()
			)
		return any(
			isinstance(record, dict)
			and isinstance(record.get("text"), str)
			and bool(record["text"].strip())
			for record in records or ()
		)
	if source == "commits":
		return any(
			isinstance(record, dict)
			and isinstance(record.get("sha"), str)
			and bool(record["sha"].strip())
			for record in records or ()
		)
	if source == "github":
		# A published release is a real history item even when its body is blank.
		return any(_isGitHubReleaseRecord(record) for record in records or ())
	return False


def _isGitHubReleaseRecord(record):
	return isinstance(record, dict) and (
		type(record.get("id")) in (int, str)
		or any(isinstance(record.get(key), str) and record[key].strip()
			for key in ("tag_name", "name", "published_at"))
	)


def _sourceItemCounts(source, model, records):
	"""Return meaningful item and document counts for an available-source label."""
	if source == "catalog":
		note = _catalogNote(model)
		version = str(getattr(model, "addonVersionName", "Latest"))
		return (len(_catalogHistory(version, note)), 0) if note else (0, 0)
	if source == "repository":
		if getattr(records, "recordKind", None) == "repositoryIndex":
			count = sum(
				isinstance(record, dict)
				and record.get("path") in REPOSITORY_CHANGELOG_PATHS
				and type(record.get("size")) is int and record["size"] > 0
				for record in records or ()
			)
			return count, count
		sections = 0
		documents = 0
		for record in records or ():
			text = record.get("text") if isinstance(record, dict) else None
			if not isinstance(text, str) or not text.strip():
				continue
			documents += 1
			history = _repositoryHistory(text)
			if history:
				sections += sum(version != _("Document introduction") for version, _notes in history)
			else:
				sections += 1
		return sections, documents
	if source == "commits":
		return sum(
			isinstance(record, dict)
			and isinstance(record.get("sha"), str)
			and bool(record["sha"].strip())
			for record in records or ()
		), 0
	if source == "github":
		return sum(_isGitHubReleaseRecord(record) for record in records or ()), 0
	return 0, 0


def _fallback(model, reason, includeCatalog=True):
	note = _catalogNote(model) if includeCatalog else ""
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
		"Repository changelog": _("Repository changelog"),
		"GitHub commit": _("GitHub commit (development history)"),
		"notGitHub": _("Release history unavailable for this source"),
		"rateLimit": _("Release service rate limit reached"),
		"networkError": _("Release service unavailable"), "missing": _("No release notes published"),
		"historyLimit": _("History retrieval limit reached"),
	}
	if isinstance(source, str) and source.startswith("catalog; "):
		return labels["catalog"] + "; " + labels.get(source.split("; ", 1)[1], _("History unavailable"))
	return labels.get(source, _("History unavailable"))


def _sourcePreference():
	"""Read the saved source choice, treating missing or invalid data as all."""
	try:
		import config
		value = config.conf["serrebiStore"].get(CHANGELOG_SOURCE_KEY, "all")
	except (ImportError, KeyError, TypeError, AttributeError):
		return "all"
	return value if value in CHANGELOG_SOURCES else "all"


def historyForModel(model, history, source=None, startPage=1, bypassCache=False):
	"""Return version, notes, provenance records without inventing history."""
	source = source if source in CHANGELOG_SOURCES else _sourcePreference()
	if source == "catalog":
		# The catalog/manifest selection must never start an Internet request.
		return _historyRows(model, [], None, source)
	repository = githubRepository(getattr(model, "sourceURL", None))
	releases, error = history.get(
		repository, "github" if source == "all" else source, startPage, bypassCache,
	)
	return _historyRows(model, releases, error, source, startPage)


def _historyRows(model, releases, error, source="all", startPage=1):
	"""Sort publication dates newest first and merge equivalent version labels."""
	source = source if source in CHANGELOG_SOURCES else "all"
	if source == "commits":
		return _commitRows(model, releases, error)
	if source == "repository":
		return _repositoryRows(model, releases, error)
	includeCatalog = source != "github"
	includeGitHub = source != "catalog"
	rows = []
	catalogVersion = str(getattr(model, "addonVersionName", "Latest"))
	catalogNote = _catalogNote(model) if includeCatalog else ""
	catalogDate = getattr(model, MODEL_RELEASE_TIME_ATTRIBUTE, None)
	catalogDate = (catalogDate / 1000 if type(catalogDate) in (int, float)
		and math.isfinite(catalogDate) and catalogDate > 0 else None)
	byVersion = {}
	if catalogNote:
		for version, notes in _catalogHistory(catalogVersion, catalogNote):
			date = catalogDate if _versionIdentity(version) == _versionIdentity(catalogVersion) else None
			identity = _versionIdentity(version)
			if identity in byVersion:
				byVersion[identity][1] += "\n\n" + notes
			else:
				byVersion[identity] = [version, notes, "catalog", date]
	withoutNotes = 0
	withNotes = 0
	for release in ((releases or []) if includeGitHub else ()):
		if not isinstance(release, dict):
			continue
		body = release.get("body")
		body = body.strip() if isinstance(body, str) else ""
		withNotes += bool(body)
		withoutNotes += not bool(body)
		version = str(release.get("tag_name") or release.get("name") or _("Unknown version"))
		date = _publicationTime(release.get("published_at"))
		identity = _versionIdentity(version)
		if identity in byVersion:
			row = byVersion[identity]
			if row[2] == "catalog":
				if body and row[1] != body:
					row[1] = _("Catalog notes:\n{catalog}\n\nGitHub release:\n{release}").format(
						catalog=row[1], release=body,
					)
				row[2] = "catalog; GitHub release"
				row[3] = date if date is not None else row[3]
		else:
			byVersion[identity] = [
				version, body or _("No release notes were published for this release."),
				"GitHub release", date,
			]
	# Known publication dates take precedence. Unknown dates follow, ordered by
	# numeric-aware labels, so v10 precedes v2 without inventing a release date.
	ordered = sorted(byVersion.values(), key=lambda row: (
		row[3] is not None, row[3] or 0, _versionSortKey(row[0]),
	), reverse=True)
	rows = [tuple(row[:3]) for row in ordered]
	if error:
		rows = [(version, notes, "catalog; %s" % error if source == "catalog" else source)
			for version, notes, source in rows]
	result = HistoryRows(rows or _fallback(model, error or "missing", includeCatalog), error)
	result.sourcePreference = source
	if rows:
		result.dates = [row[3] for row in ordered]
	result.omitted = sum(isinstance(release, dict) and not (
		isinstance(release.get("body"), str) and release["body"].strip()
	) for release in (releases or []) if includeGitHub)
	result.withNotes = withNotes
	result.withoutNotes = withoutNotes
	result.retrievedCount = withNotes + withoutNotes
	result.usedCached = bool(getattr(releases, "usedCached", False))
	result.limitReached = bool(getattr(releases, "limitReached", False))
	result.nextPage = getattr(releases, "nextPage", None)
	result.hasGitHub = includeGitHub and error != "notGitHub" \
		and githubRepository(getattr(model, "sourceURL", None)) is not None
	return result


def _commitRows(model, commits, error):
	"""Format GitHub commits as explicitly labeled development history."""
	rows = []
	dates = []
	seen = set()
	for commit in commits or []:
		if not isinstance(commit, dict):
			continue
		sha = commit.get("sha")
		message = commit.get("message")
		if not isinstance(sha, str) or not sha or sha in seen:
			continue
		seen.add(sha)
		message = message.strip() if isinstance(message, str) else ""
		shortSha = sha[:7]
		subject = message.splitlines()[0] if message else _("No commit message was published")
		rows.append((_("Commit {sha}: {subject}").format(sha=shortSha, subject=subject),
			message or _("No commit message was published for this commit."), "GitHub commit"))
		dates.append(_publicationTime(commit.get("date")))
	if rows:
		displayRows = rows
	else:
		# Translators: No commit records were returned for the selected repository.
		displayRows = [("", _("No GitHub commit history is available for this add-on."), error or "missing")]
	result = HistoryRows(displayRows, error)
	result.sourcePreference = "commits"
	result.recordKind = "commits"
	if rows:
		result.dates = dates
	result.withNotes = sum(bool(isinstance(item.get("message"), str) and item["message"].strip())
		for item in (commits or []) if isinstance(item, dict))
	result.withoutNotes = len(rows) - result.withNotes
	result.retrievedCount = len(rows)
	result.usedCached = bool(getattr(commits, "usedCached", False))
	result.limitReached = bool(getattr(commits, "limitReached", False))
	result.nextPage = getattr(commits, "nextPage", None)
	result.hasGitHub = error != "notGitHub" \
		and githubRepository(getattr(model, "sourceURL", None)) is not None
	return result


def _repositoryRows(model, documents, error):
	"""Expose authored repository changelog sections without calling them releases."""
	rows = []
	for document in documents or []:
		if not isinstance(document, dict):
			continue
		path = document.get("path")
		text = document.get("text")
		if not isinstance(path, str) or not isinstance(text, str):
			continue
		sections = _repositoryHistory(text)
		if sections:
			rows.extend((version, notes, "Repository changelog") for version, notes in sections)
		elif text.strip():
			# Keep an authored document even when it makes no conservative version claims.
			rows.append((_("Unsectioned document: {path}").format(path=path), text.strip(),
				"Repository changelog"))
	if rows:
		displayRows = rows
	else:
		# Translators: Supported repository changelog files were absent or empty.
		displayRows = [("", _("No repository changelog was found in the supported paths."),
			error or "missing")]
	result = HistoryRows(displayRows, error)
	result.sourcePreference = "repository"
	result.recordKind = "repository"
	result.retrievedCount = len(rows)
	result.documentCount = sum(isinstance(item, dict) and isinstance(item.get("text"), str)
		for item in (documents or []))
	result.usedCached = bool(getattr(documents, "usedCached", False))
	result.limitReached = bool(getattr(documents, "limitReached", False))
	result.nextPage = getattr(documents, "nextPage", None)
	result.hasGitHub = error != "notGitHub" \
		and githubRepository(getattr(model, "sourceURL", None)) is not None
	return result


def _repositoryHistory(text):
	"""Split generic explicit version headings, retaining every authored byte as text."""
	heading = re.compile(
		r"^(?:#{1,6}\s+)?(?:Changes\s+for\s+|Version\s+)?\[?"
		r"(v?\d+\.\d+(?:\.\d+)*(?:[-.][A-Za-z][A-Za-z0-9.-]*)?)\]?"
		r"(?:\s*(?:[-:]\s*\d{4}-\d{2}-\d{2}|\([^\n]*\)))?:?\s*$",
		re.MULTILINE | re.IGNORECASE,
	)
	matches = list(heading.finditer(text))
	if not matches:
		return []
	rows = []
	preamble = text[:matches[0].start()].strip()
	for index, match in enumerate(matches):
		end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
		rows.append((match.group(1), text[match.start():end].strip()))
	rows.sort(key=lambda row: _versionSortKey(row[0]), reverse=True)
	if preamble:
		rows.append((_("Document introduction"), preamble))
	return rows


def _historyItemLabel(rows, index):
	version, _notes, source = rows[index]
	date = rows.dates[index] if isinstance(rows, HistoryRows) else None
	try:
		dateText = datetime.fromtimestamp(date, timezone.utc).strftime("%Y-%m-%d (UTC)") if date else None
	except (ValueError, OverflowError, OSError):
		dateText = None
	unknownDate = _("Commit date unknown") if source == "GitHub commit" else _("Release date unknown")
	# Translators: One changelog history row with version, release date, and source.
	return _("{version}; {date}; {source}").format(
		version=version or _("Notes"), date=dateText or unknownDate,
		source=_provenanceLabel(source),
	)


def _catalogHistory(currentVersion, note):
	"""Split only unambiguous, standalone version headings; retain other text."""
	heading = re.compile(
		r"^(?:#{1,6}\s+)?(v?\d+\.\d+(?:\.\d+)*(?:[-.][A-Za-z][A-Za-z0-9.-]*)?)"
		r"(?:\s+\([^\n]*\))?:?\s*$", re.MULTILINE,
	)
	matches = list(heading.finditer(note))
	if len(matches) < 2:
		return [(currentVersion, note)]
	rows = []
	preamble = note[:matches[0].start()].strip()
	for index, match in enumerate(matches):
		end = matches[index + 1].start() if index + 1 < len(matches) else len(note)
		body = note[match.end():end].strip()
		if body:
			rows.append((match.group(1), body))
	if preamble:
		# Unversioned introductory text must not disappear during section splitting.
		rows.insert(0, (currentVersion, preamble))
	return rows or [(currentVersion, note)]


def _publicationTime(value):
	if not isinstance(value, str):
		return None
	try:
		date = datetime.fromisoformat(value.replace("Z", "+00:00"))
		return date.timestamp() if date.tzinfo is not None else None
	except (ValueError, OverflowError, OSError):
		return None


def _versionIdentity(version):
	version = re.sub(r"^v(?=\d)", "", version.strip().casefold())
	match = re.fullmatch(r"(\d+(?:\.\d+)*)([.-]?[a-z][a-z0-9.-]*)?", version)
	if not match:
		return version
	parts = [int(part) for part in match[1].split(".")]
	while len(parts) > 1 and parts[-1] == 0:
		parts.pop()
	return ".".join(str(part) for part in parts) + (match[2] or "")


def _versionSortKey(version):
	version = _versionIdentity(version)
	match = re.fullmatch(r"(\d+(?:\.\d+)*)([.-]?[a-z][a-z0-9.-]*)?", version)
	def natural(value):
		return tuple((1, int(part)) if part.isdigit() else (0, part)
			for part in re.split(r"(\d+)", value) if part)
	if match:
		return (1, tuple(int(part) for part in match[1].split(".")), not bool(match[2]), natural(match[2] or ""))
	return (0, (), False, natural(version))


def _historyStatus(rows):
	preference = getattr(rows, "sourcePreference", "all")
	if preference == "commits":
		status = _("{count} GitHub commits retrieved as development history.").format(
			count=rows.retrievedCount,
		)
		status += " " + _("These are commits, not published releases.")
		if rows.withoutNotes:
			status += " " + _("{count} commits have no published message.").format(count=rows.withoutNotes)
	elif preference == "github":
		status = _("{count} GitHub published releases retrieved; {withNotes} have notes and "
			"{withoutNotes} have no published notes.").format(
				count=rows.withNotes + rows.withoutNotes,
				withNotes=rows.withNotes, withoutNotes=rows.withoutNotes,
			)
		status += " " + _("Source: GitHub published releases.")
	elif preference == "repository":
		status = _("{count} authored changelog sections from {documents} repository documents.").format(
			count=rows.retrievedCount, documents=rows.documentCount,
		)
		status += " " + _("Source: repository changelog files; these are not GitHub release records.")
	else:
		# Translators: Count of available changelog records, not a count of all releases.
		status = _("{count} versions with available notes.").format(
			count=sum(source in ("catalog", "GitHub release", "catalog; GitHub release")
				or str(source).startswith("catalog; ") for _version, _notes, source in rows),
		)
	if preference == "catalog":
		status += " " + _("Source: catalog or manifest notes.")
	elif preference == "all" and getattr(rows, "hasGitHub", False):
		status += " " + _("Sources: catalog or manifest notes, and GitHub published releases.")
		status += " " + _("{withNotes} GitHub published releases have notes; "
			"{withoutNotes} have no published notes.").format(
				withNotes=rows.withNotes, withoutNotes=rows.withoutNotes,
			)
	elif preference not in ("github", "repository", "commits"):
		status += " " + _("Source: catalog or manifest notes.")
	if getattr(rows, "error", None):
		status += " " + _("History is incomplete: {reason}. Available history records are retained.").format(
			reason=_provenanceLabel(rows.error),
		)
	if getattr(rows, "usedCached", False):
		status += " " + _("Cached GitHub history retained because the refresh was incomplete.")
	if getattr(rows, "limitReached", False) and rows.error != "historyLimit":
		status += " " + _("History retrieval limit reached; additional records were omitted.")
	if getattr(rows, "nextPage", None):
		status += " " + (
			_("Another repository changelog file is available with the Read next changelog file button.")
			if preference == "repository"
			else _("Older history is available with the Load older history button."))
	return status


def enableSettings(plugin, settingsPanel):
	"""Add the persisted changelog-source choice to the add-on settings panel.

	This is deliberately separate from :meth:`ChangelogFeature.enable`: the
	feature can be active before the settings panel is registered. Specific saved
	sources open directly; the saved all-sources choice asks on each opening.
	"""
	if isSecureDesktop():
		return
	import config
	import wx
	spec = getattr(config.conf, "spec", None)
	if spec is not None:
		try:
			spec.setdefault("serrebiStore", {})[CHANGELOG_SOURCE_KEY] = (
				"option('all', 'catalog', 'github', 'repository', 'commits', default='all')"
			)
		except AttributeError:
			spec["serrebiStore"][CHANGELOG_SOURCE_KEY] = (
				"option('all', 'catalog', 'github', 'repository', 'commits', default='all')"
			)
	originalSettings = settingsPanel.makeSettings
	originalSave = settingsPanel.onSave
	def makeSettings(panel, sizer):
		originalSettings(panel, sizer)
		from gui import guiHelper
		helper = guiHelper.BoxSizerHelper(panel, sizer=sizer)
		# Translators: Chooses where the changelog viewer looks for release notes.
		panel._changelogSourceChoice = helper.addLabeledControl(
			_("Changelog &source:"), wx.Choice,
			choices=[
				# Translators: Include catalog/manifest notes and GitHub release notes when available.
				_("All available sources"),
				# Translators: Do not use the network; read catalog or installed-manifest notes only.
				_("Catalog or installed manifest notes"),
				# Translators: Read release notes published on GitHub only.
				_("GitHub release notes"),
				_("Repository changelog files"),
				# Translators: Read repository commits as development history, not releases.
				_("GitHub commits (development history, not releases)"),
			],
		)
		panel._changelogSourceChoice.SetSelection(
			CHANGELOG_SOURCES.index(_sourcePreference())
		)
	def saveSettings(panel):
		originalSave(panel)
		if not hasattr(panel, "_changelogSourceChoice"):
			return
		try:
			settings = config.conf["serrebiStore"]
		except KeyError:
			settings = config.conf.setdefault("serrebiStore", {})
		selection = panel._changelogSourceChoice.GetSelection()
		settings[CHANGELOG_SOURCE_KEY] = CHANGELOG_SOURCES[
			selection if 0 <= selection < len(CHANGELOG_SOURCES) else 0
		]
	plugin._rememberPatch(settingsPanel, "makeSettings", makeSettings)
	plugin._rememberPatch(settingsPanel, "onSave", saveSettings)


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
					(LAST_UPDATED_TIME_KEY, MODEL_LAST_UPDATED_TIME_ATTRIBUTE),
				):
					value = data.get(key)
					if isinstance(value, (str, int, float)):
						object.__setattr__(model, attribute, value)
				publication, source = _provenFirstPublication(data)
				if publication is not None and source is not None:
					object.__setattr__(model, MODEL_ORIGINAL_PUBLICATION_TIME_ATTRIBUTE, publication)
					object.__setattr__(model, MODEL_ORIGINAL_PUBLICATION_SOURCE_ATTRIBUTE, source)
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
				(LAST_UPDATED_TIME_KEY, MODEL_LAST_UPDATED_TIME_ATTRIBUTE),
				(ORIGINAL_PUBLICATION_TIME_KEY, MODEL_ORIGINAL_PUBLICATION_TIME_ATTRIBUTE),
				(ORIGINAL_PUBLICATION_SOURCE_KEY, MODEL_ORIGINAL_PUBLICATION_SOURCE_ATTRIBUTE),
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
		publicationField = listModule.AddonListField.publicationDate
		self.plugin._rememberPatch(publicationField, "displayString", _("Last updated"))
		modelModule = importlib.import_module("addonStore.models.addon")
		storeModel = modelModule._AddonStoreModel
		self.plugin._rememberPatch(storeModel, "publicationDate", property(
			lambda model: _formattedDate(updatedTime(model)),
		))
		originalFieldText = cls._getAddonFieldText
		def fieldText(vm, item, field):
			if getattr(field, "name", None) == "publicationDate":
				return _formattedDate(updatedTime(item.model)) or _("Unknown")
			return originalFieldText(vm, item, field)
		self.plugin._rememberPatch(cls, "_getAddonFieldText", fieldText)
		original = cls._getFilteredSortedIds
		def filtered(vm):
			dateSort = getattr(vm, "_serrebiDateSort", None)
			publication = getattr(getattr(vm, "_sortByModelField", None), "name", None) == "publicationDate"
			# Native ordering remains authoritative for membership, filters,
			# relevance and every known value.  Stable partitioning only moves
			# absent values after known values, even for descending sorts.
			nativeOrder = original(vm)
			items = [vm._addons[addonId] for addonId in nativeOrder]
			if dateSort is None and not publication:
				field = vm._sortByModelField
				if getattr(field, "name", None) == "searchRank":
					return nativeOrder
				known = []
				unknown = []
				for item in items:
					(unknown if _unknownSortText(vm._getAddonFieldText(item, field)) else known).append(item.Id)
				return known + unknown
			# submissionTime is NVDA's current-version catalog timestamp.  It is
			# shown as Last updated without claiming it is a source-code commit.
			# The former publication field and the persisted custom date sort now
			# show the same useful catalog version date.
			return [
				item.Id
				for item in sorted(items, key=lambda item: sortKey(
					item.model, vm._reverseSort if dateSort is None else dateSort, item.Id,
				))
			]
		self.plugin._rememberPatch(cls, "_getFilteredSortedIds", filtered)
		originalSetSort = cls.setSortField
		def setSortField(vm, *args, **kwargs):
			vm._serrebiDateSort = None
			return originalSetSort(vm, *args, **kwargs)
		self.plugin._rememberPatch(cls, "setSortField", setSortField)
		def getChoices(vm):
			result = list(choices.__get__(vm, type(vm)))
			# Translators: An Add-on Store sort option using the native current-version timestamp.
			result.append(_("Last updated (ascending)"))
			# Translators: An Add-on Store sort option using the native current-version timestamp.
			result.append(_("Last updated (descending)"))
			return result
		self.plugin._rememberPatch(cls, "_columnSortChoices", property(getChoices))
		self._patchDateDetails(importlib, modelModule)

	def _patchDateDetails(self, importlib, modelModule):
		"""Show the supplied catalog version date, without an unprovable first date."""
		try:
			detailsModule = importlib.import_module("gui.addonStoreGui.controls.details")
		except ImportError:
			return
		cls = getattr(detailsModule, "AddonDetails", None)
		if cls is None or not hasattr(cls, "_refresh"):
			return
		original = cls._refresh
		storeModel = modelModule._AddonStoreModel
		def refresh(view):
			if getattr(view, "_isBeingDestroyed", False):
				return
			details = None if view._detailsVM.listItem is None else view._detailsVM.listItem.model
			append = view._appendDetailsLabelValue
			nativeTranslate = getattr(detailsModule, "pgettext", getattr(builtins, "pgettext", None))
			nativeLabel = (nativeTranslate("addonStore", "Publication date:")
				if nativeTranslate else _("Publication date:"))
			hadOverride = "_appendDetailsLabelValue" in vars(view)
			dateShown = False
			def correctedAppend(label, value):
				nonlocal dateShown
				if label == nativeLabel:
					label = _("Last updated:")
					dateShown = True
				return append(label, value)
			view._appendDetailsLabelValue = correctedAppend
			try:
				original(view)
			finally:
				if hadOverride:
					view._appendDetailsLabelValue = append
				else:
					del view._appendDetailsLabelValue
			if getattr(view, "_isBeingDestroyed", False):
				return
			if not isinstance(details, storeModel):
				return
			if not dateShown:
				append(_("Last updated:"), _formattedDate(updatedTime(details)) or _("Unknown"))
			append(_("Date meaning:"), _(
				"Date supplied by the catalog: release, submission, file modification or repository activity date, "
				"depending on the source. Some sources fall back to repository creation. "
				"It does not guarantee the exact last update or first-ever release date."
			))
		self.plugin._rememberPatch(cls, "_refresh", refresh)

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
			# The native choice count differs by NVDA version (for example Rank is
			# absent on older versions), so the appended choices cannot use parity.
			vm._serrebiDateSort = selection == choiceCount - 1
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
		source = _sourcePreference()
		if source == "all":
			self._discoverSources(item.model, item.model.displayName, dialogRef)
			return
		self._requestHistory(item.model, item.model.displayName, dialogRef, source)

	def _discoverSources(self, model, name, dialogRef):
		"""Offer local notes and safe GitHub capabilities without probing the network."""
		generation = self._generation
		self._requestGeneration += 1
		requestGeneration = self._requestGeneration
		parent = dialogRef()
		if parent is None or not getattr(parent, "_serrebiChangelogAlive", False) or isSecureDesktop():
			return
		repository = githubRepository(getattr(model, "sourceURL", None))
		sources = []
		if _catalogNote(model):
			sources.append(("catalog", None, 0, False))
		if repository:
			sources.extend((source, None, 0, False)
				for source in ("github", "repository", "commits"))
		if not sources:
			import ui
			ui.message(_("No changelog sources are available for this add-on."))
			return
		source = self._chooseSource(parent, tuple(sources))
		parent = dialogRef()
		if (source is not None and generation == self._generation
			and requestGeneration == self._requestGeneration and parent is not None
			and getattr(parent, "_serrebiChangelogAlive", False) and not isSecureDesktop()):
			self._requestHistory(
				model, name, dialogRef, source,
				discovered=([], None) if source == "catalog" else None,
			)

	def _chooseSource(self, parent, sources, failures=()):
		"""Ask on each opening only when the saved preference is all sources."""
		import wx
		labels = []
		for source, count, documents, partial in sources:
			prefix = (_("at least {count}").format(count=count) if partial else str(count)) \
				if count is not None else None
			if count is None and source == "catalog":
				label = _("Catalog or installed manifest notes")
			elif count is None and source == "github":
				label = _("GitHub published releases")
			elif count is None and source == "repository":
				label = _("Repository changelog files")
			elif count is None:
				label = _("GitHub commits (development history, not releases)")
			elif source == "catalog":
				label = _("Catalog or installed manifest notes ({count} versions)").format(count=prefix)
			elif source == "github":
				label = _("GitHub published releases ({count} releases)").format(count=prefix)
			elif source == "repository":
				label = _("Repository changelog files ({count} files)").format(count=prefix)
			else:
				label = _("GitHub commits ({count} commits; development history, not releases)").format(
					count=prefix,
				)
			if partial:
				label += " " + _("Partial retrieval.")
			labels.append(label)
		# Translators: Prompt shown when the saved changelog source is All available sources.
		prompt = _("Choose which source to load for this changelog. GitHub content loads only after selection.")
		if failures:
			# Translators: Source choices remain usable, but other sources could not be verified.
			prompt += " " + _("Some unavailable sources could not be verified: {reasons}.").format(
				reasons="; ".join(failures),
			)
		dialog = wx.SingleChoiceDialog(
			parent,
			prompt,
			_("Changelog source"),
			labels,
		)
		try:
			if dialog.ShowModal() != wx.ID_OK:
				return None
			selection = dialog.GetSelection()
			return sources[selection][0] if 0 <= selection < len(sources) else None
		finally:
			dialog.Destroy()

	def _requestHistory(self, model, name, dialogRef, source, startPage=1, discovered=None):
		import ui
		# Translators: Release history is being retrieved in the background.
		ui.message(_("Loading changelog."))
		generation = self._generation
		self._requestGeneration += 1
		requestGeneration = self._requestGeneration
		def fetched(releases, error):
			rows = _historyRows(model, releases, error, source, startPage)
			if generation != self._generation or requestGeneration != self._requestGeneration:
				return
			try:
				import wx
				wx.CallAfter(
					self._showDialog, model, name, rows, source,
					generation, requestGeneration, dialogRef,
				)
			except Exception:
				return
		if discovered is not None:
			fetched(*discovered)
		elif source == "catalog":
			# Catalog/manifest notes are already in the selected model.
			fetched([], None)
		else:
			self.history.getAsync(
				githubRepository(getattr(model, "sourceURL", None)), fetched,
				source, startPage, False,
			)

	def _showDialog(self, model, name, rows, source, generation, requestGeneration, dialogRef):
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
		# Translators: Label for the history-record list in the changelog viewer.
		listLabel = (_("&Commit, date and source:") if source == "commits"
			else _("&Version, release date and source:"))
		sizer.Add(wx.StaticText(dialog, label=listLabel), 0, wx.ALL, 8)
		choice = wx.ListBox(dialog, choices=[
			_historyItemLabel(rows, index) for index in range(len(rows))
		])
		sizer.Add(choice, 1, wx.EXPAND | wx.ALL, 8)
		# Translators: Label for the read-only history details.
		notesLabel = _("Commit &message:") if source == "commits" else _("Release &notes:")
		sizer.Add(wx.StaticText(dialog, label=notesLabel), 0, wx.LEFT | wx.RIGHT, 8)
		text = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
		def choose(_evt=None):
			selection = choice.GetSelection()
			if selection != wx.NOT_FOUND:
				text.ChangeValue(rows[selection][1])
				text.SetInsertionPoint(0)
		def onKey(event):
			key = event.GetKeyCode()
			focus = wx.Window.FindFocus()
			if key == wx.WXK_ESCAPE:
				if focus is text:
					choice.SetFocus()
				else:
					dialog.EndModal(wx.ID_CANCEL)
			else:
				event.Skip()
		choice.Bind(wx.EVT_LISTBOX, choose)
		dialog.Bind(wx.EVT_CHAR_HOOK, onKey)
		choice.SetSelection(0)
		choose()
		sizer.Add(text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
		# Translators: Label for a keyboard-accessible retrieval status, separate from notes.
		sizer.Add(wx.StaticText(dialog, label=_("History &status:")), 0, wx.LEFT | wx.RIGHT, 8)
		status = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 70))
		status.ChangeValue(_historyStatus(rows))
		sizer.Add(status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		loadOlder = [False]
		if rows.nextPage:
			# Translators: Fetch one more repository file, or one older release/commit page.
			olderLabel = (_("Read &next changelog file") if source == "repository"
				else _("Load &older history"))
			older = wx.Button(dialog, label=olderLabel)
			def onOlder(_evt):
				loadOlder[0] = True
				dialog.EndModal(wx.ID_CANCEL)
			older.Bind(wx.EVT_BUTTON, onOlder)
			buttons.Add(older, 0)
		close = wx.Button(dialog, wx.ID_CANCEL, label=_("&Close"))
		close.Bind(wx.EVT_BUTTON, lambda _evt: dialog.EndModal(wx.ID_CANCEL))
		buttons.Add(close, 0)
		sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
		dialog.SetSizerAndFit(sizer)
		dialog.SetSize((750, 600))
		choice.SetFocus()
		try:
			dialog.ShowModal()
		finally:
			dialog.Destroy()
		if loadOlder[0]:
			self._requestHistory(model, name, dialogRef, source, rows.nextPage)
