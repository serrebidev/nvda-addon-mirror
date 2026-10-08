"""Local, deterministic Add-on Store discovery and repository helpers.

This module deliberately does not import NVDA GUI modules.  That keeps the
matching and git safety rules testable and prevents the plugin loader treating
it as a second global plugin.
"""

from __future__ import annotations

import ntpath
import os
import re
import subprocess
from collections import Counter
from urllib.parse import urlparse


_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)
_STOP_WORDS = frozenset({
	"addon", "add-on", "and", "for", "from", "into", "nvda", "the", "this", "with",
})

# These values cross the boundary into addonStoreMirror.py.  Keep them as
# symbols rather than English UI text: the helper owns translation while this
# module deliberately remains independent from NVDA's GUI imports.
AUTHOR_PUBLISHER = "authorPublisher"
REPOSITORY_OWNER = "repositoryOwner"
SIMILAR_TITLE = "title"
SIMILAR_DESCRIPTION = "description"
SIMILAR_TERMS = "titleDescriptionTerms"
SIMILAR_SCORE = "score"


class CloneFailure(RuntimeError):
	"""A clone failure represented by a UI-localizable symbolic code."""

	def __init__(self, code):
		super().__init__(code)
		self.code = code


def safeHttpsURL(value):
	"""Return an HTTPS URL suitable for opening or cloning, otherwise None."""
	if not isinstance(value, str) or any(ord(char) < 32 for char in value):
		return None
	try:
		parsed = urlparse(value.strip())
	except ValueError:
		return None
	if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
		return None
	return value.strip()


def githubRepository(value):
	"""Return canonical owner/repository for an HTTPS GitHub repository URL."""
	value = safeHttpsURL(value)
	if value is None:
		return None
	parsed = urlparse(value)
	try:
		if parsed.port not in (None, 443) or parsed.query or parsed.fragment:
			return None
	except ValueError:
		return None
	if parsed.hostname is None or parsed.hostname.casefold() != "github.com":
		return None
	parts = [part for part in parsed.path.split("/") if part]
	if len(parts) != 2:
		return None
	owner, repository = parts
	if repository.endswith(".git"):
		repository = repository[:-4]
	if not owner or not repository:
		return None
	if not re.fullmatch(r"[\w.-]+", owner) or not re.fullmatch(r"[\w.-]+", repository):
		return None
	return owner.casefold(), repository.casefold()


def repositoryURL(model):
	"""Return the verified GitHub source URL carried by a store model."""
	url = safeHttpsURL(getattr(model, "sourceURL", None))
	return url if githubRepository(url) else None


def repositoryDisplay(model):
	"""Return the source's original-case GitHub owner and repository names."""
	url = repositoryURL(model)
	if url is None:
		return None
	parts = [part for part in urlparse(url).path.split("/") if part]
	owner, repository = parts
	if repository.endswith(".git"):
		repository = repository[:-4]
	return owner, repository


def displayName(model):
	value = getattr(model, "displayName", "")
	return value.strip() if isinstance(value, str) and value.strip() else str(getattr(model, "addonId", ""))


def _identity(model):
	for name in ("author", "publisher"):
		value = getattr(model, name, None)
		if isinstance(value, str) and value.strip():
			return value.strip()
	return None


def catalogAuthor(model):
	"""Return only the catalog's author or publisher metadata."""
	return _identity(model)


def authorName(model):
	"""Return catalog author/publisher, falling back to the GitHub owner."""
	identity = catalogAuthor(model)
	if identity:
		return identity
	repository = repositoryDisplay(model)
	return repository[0] if repository else None


def authorMatches(selectedModel, models):
	"""Find distinct loaded add-ons sharing author/publisher or repository owner."""
	identity = _identity(selectedModel)
	selectedRepo = githubRepository(getattr(selectedModel, "sourceURL", None))
	selectedOwner = selectedRepo[0] if selectedRepo else None
	results = []
	seen = set()
	for model in models:
		addonId = str(getattr(model, "addonId", "")).casefold()
		if not addonId or addonId in seen:
			continue
		candidateIdentity = _identity(model)
		candidateRepo = githubRepository(getattr(model, "sourceURL", None))
		evidence = []
		if identity and candidateIdentity and identity.casefold() == candidateIdentity.casefold():
			evidence.append((AUTHOR_PUBLISHER, ()))
		if selectedOwner and candidateRepo and selectedOwner == candidateRepo[0]:
			evidence.append((REPOSITORY_OWNER, ()))
		if evidence:
			seen.add(addonId)
			results.append((model, tuple(evidence)))
	return results


def _tokens(value):
	if not isinstance(value, str):
		return []
	return [
		token.casefold()
		for token in _TOKEN_RE.findall(value)
		if len(token) > 2 and token.casefold() not in _STOP_WORDS
	]


def similarMatches(selectedModel, models, limit=12):
	"""Return deterministic weighted title/description similarity matches."""
	selectedId = str(getattr(selectedModel, "addonId", "")).casefold()
	selectedTitle = Counter(_tokens(getattr(selectedModel, "displayName", "")))
	selectedDescription = Counter(_tokens(getattr(selectedModel, "description", "")))
	selectedWeights = Counter(selectedDescription)
	selectedWeights.update(
		{token: count * 3 for token, count in selectedTitle.items()},
	)
	results = []
	seen = set()
	for model in models:
		addonId = str(getattr(model, "addonId", "")).casefold()
		if not addonId or addonId == selectedId or addonId in seen:
			continue
		title = Counter(_tokens(getattr(model, "displayName", "")))
		description = Counter(_tokens(getattr(model, "description", "")))
		weights = Counter(description)
		weights.update({token: count * 3 for token, count in title.items()})
		shared = sorted(set(selectedWeights) & set(weights))
		score = sum(min(selectedWeights[token], weights[token]) for token in shared)
		if not score:
			continue
		# A catalog can contain stable and development channels for one add-on.
		# Only a positive channel match may suppress a later matching channel.
		seen.add(addonId)
		reasons = []
		sharedTitle = sorted(set(selectedTitle) & set(title))
		sharedDescription = sorted(set(selectedDescription) & set(description))
		if sharedTitle:
			reasons.append((SIMILAR_TITLE, tuple(sharedTitle[:4])))
		if sharedDescription:
			reasons.append((SIMILAR_DESCRIPTION, tuple(sharedDescription[:4])))
		if not reasons:
			reasons.append((SIMILAR_TERMS, tuple(shared[:4])))
		results.append((score, displayName(model).casefold(), addonId, model, tuple(reasons)))
	results.sort(key=lambda item: (-item[0], item[1], item[2]))
	return [(item[3], item[4], item[0]) for item in results[:limit]]


def cloneRepository(url, destination, timeout=120):
	"""Clone a verified GitHub repository with an argument vector and bounded wait."""
	url = repositoryURL(type("Model", (), {"sourceURL": url})())
	if url is None:
		raise ValueError("A verified HTTPS GitHub repository is required.")
	# The add-on only runs on Windows, where os.path.isabs accepts drive paths.
	# Its tests run on POSIX CI too, so also accept Windows-style absolute
	# destinations there; the refusal of existing folders is unchanged.
	if (
		not isinstance(destination, str)
		or not (os.path.isabs(destination) or ntpath.isabs(destination))
		or os.path.exists(destination)
	):
		raise ValueError("Choose a new, empty destination folder.")
	try:
		completed = subprocess.run(
			["git", "clone", "--", url, destination],
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			timeout=timeout,
			check=False,
			creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
		)
	except subprocess.TimeoutExpired as error:
		raise CloneFailure("timeout") from error
	except OSError as error:
		raise CloneFailure("gitUnavailable") from error
	if completed.returncode:
		raise CloneFailure("cloneFailed")
	return destination
