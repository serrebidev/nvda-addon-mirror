"""Runs mirror.main() for three real builds and installs what it publishes.

The catalogs and the internet are faked; everything between them is the real
pipeline, and tests/nvda_contract.py plays NVDA at the far end. A row that the
real store would refuse fails here, whichever stage produced it.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from email.message import Message
from unittest import mock
from urllib.error import HTTPError, URLError

import mirror
from tests import nvda_contract

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_800_000_000
HOUR = 3600
API_VERSIONS = ("2027.1.0", "2026.3.0", "2025.1.0")
BACK_COMPAT = {"2027.1.0": (2027, 1, 0), "2026.3.0": (2026, 1, 0), "2025.1.0": (2024, 1, 0)}
API_TUPLE = {"2027.1.0": (2027, 1, 0), "2026.3.0": (2026, 3, 0), "2025.1.0": (2025, 1, 0)}
#: Small, so a test bundle can be "oversized" without being 40 MB.
CAPTURE_LIMIT = 256 * 1024
PADDING = random.Random(2).randbytes(CAPTURE_LIMIT * 2)
BASE = "https://host.invalid/"
SITE = "https://site.invalid/mirror"


def manifest_text(name, version="1.0", min_nvda="2025.1", last="2027.1", extra=""):
    lines = [f"name = {name}", 'summary = "Summary"', 'author = "Author"',
             f"version = {version}"]
    if min_nvda is not None:
        lines.append(f"minimumNVDAVersion = {min_nvda}")
    if last is not None:
        lines.append(f"lastTestedNVDAVersion = {last}")
    return "\n".join(lines) + "\n" + extra


def bundle(text, oversized=False):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        if oversized:
            archive.writestr("data.bin", PADDING)
        archive.writestr("manifest.ini", text)
    return out.getvalue()


class Response(io.BytesIO):
    def __init__(self, body, status, url, headers):
        super().__init__(body)
        self.status = status
        self.url = url
        self.headers = headers

    def geturl(self):
        return self.url


class Internet:
    """Every URL a build may touch, with a log of how it touched each."""

    def __init__(self):
        self.files = {}
        self.calls = []
        self.unexpected = []
        self.lock = threading.Lock()

    def serve(self, url, raw, etag='"1"', ranges=True, down=False):
        self.files[url] = {"raw": raw, "etag": etag, "ranges": ranges, "down": down}

    def count(self, url, kind, start=0):
        return sum(1 for u, k in self.calls[start:] if u == url and k == kind)

    def kinds(self, start=0):
        return [k for _, k in self.calls[start:]]

    def __call__(self, request, timeout=None):
        url = request.full_url
        spec = request.get_header("Range")
        with self.lock:
            served = self.files.get(url)
            if served is None:
                self.unexpected.append(url)
                raise HTTPError(url, 404, "Not Found", Message(), io.BytesIO())
            if served["down"]:
                self.calls.append((url, "down"))
                raise URLError("timed out")
            sent = request.get_header("If-none-match")
            headers = {"ETag": served["etag"]} if served["etag"] else {}
            if sent and sent == served["etag"]:
                self.calls.append((url, "304"))
                raise HTTPError(url, 304, "Not Modified", Message(), io.BytesIO())
            if spec and served["ranges"]:
                self.calls.append((url, "range"))
                first, last = spec.removeprefix("bytes=").split("-")
                return Response(
                    served["raw"][int(first):int(last) + 1], 206, url, headers,
                )
            self.calls.append((url, "refused" if spec else "full"))
            return Response(served["raw"], 200, url, headers)


def row(name, url, version="1.0", source="bestmidi", channel="stable",
        min_nvda=(2025, 1, 0), last=(2027, 1, 0), **extra):
    return dict({
        "name": name, "summary": name, "description": "Description.",
        "author": "Author", "version": version, "channel": channel,
        "homepage": "", "source_url": "https://source.invalid/", "license": "GPL",
        "license_url": "", "changelog": "", "download_url": url,
        "submission_ms": 1, "min_nvda": min_nvda, "last_tested": last,
        "source": source,
    }, **extra)


class Scenario:
    """The internet and catalogs every test below reads the results of."""

    def __init__(self):
        self.internet = Internet()
        self.seed = {}
        self.official, self.bestmidi, self.ru, self.es = [], [], [], []
        self.owners, self.pinned = [], []
        serve = self.internet.serve

        # Every source must reach the far end, not only the catalogs above.
        self.es_url = BASE + "esAddon.nvda-addon"
        serve(self.es_url, bundle(manifest_text("esAddon")))
        self.es.append(row("esAddon", self.es_url, source="es"))
        self.owner_url = BASE + "ownerAddon.nvda-addon"
        serve(self.owner_url, bundle(manifest_text("ownerAddon")))
        self.owners.append(row("Owner Display", self.owner_url, source="github_owner"))
        # A pinned bundle is repackaged and hosted by the mirror itself.
        pinned = bundle(manifest_text("pinnedAddon", "2.0"))
        self.pinned_url = f"{SITE}/downloads/pinnedAddon-2.0.nvda-addon"
        serve(self.pinned_url, pinned)
        self.pinned.append(row(
            "pinnedAddon", BASE + "upstream.nvda-addon", "2.0", source="pinned",
            sha256=hashlib.sha256(pinned).hexdigest(), _patched_bytes=pinned,
        ))

        # Two authors' add-ons sharing one manifest name. The fork is pinned
        # under its own ID; a catalog row for the fork's repo must not take
        # the shared name from the original, even from a stronger source.
        self.shared = BASE + "shared.nvda-addon"
        serve(self.shared, bundle(manifest_text("sharedAddon", "1.0")))
        self.bestmidi.append(row("sharedAddon", self.shared, "1.0"))
        self.fork_repo = "https://github.com/forkOwner/shared"
        self.fork_release = self.fork_repo + "/releases/download/v2/shared.nvda-addon"
        serve(self.fork_release, bundle(manifest_text("sharedAddon", "2.0")))
        self.ru.append(row("sharedAddon", self.fork_release, "2.0", source="ru"))
        fork = bundle(manifest_text("forkOwner-sharedAddon", "2.0"))
        self.fork_hosted = f"{SITE}/downloads/forkOwner-sharedAddon-2.0.nvda-addon"
        serve(self.fork_hosted, fork)
        self.pinned.append(row(
            "forkOwner-sharedAddon", self.fork_release, "2.0", source="pinned",
            source_url=self.fork_repo, sha256=hashlib.sha256(fork).hexdigest(),
            _patched_bytes=fork,
        ))

        # The report that started this: BestMidi's display name as the ID, a
        # 40 MB bundle over the capture limit, and a beta row for the release
        # the official store carries as stable.
        self.vision = BASE + "VisionAssistant-2026.10.22.nvda-addon"
        vision = bundle(manifest_text(
            "VisionAssistant", "2026.10.22", "2025.1", "2026.3",
        ), oversized=True)
        serve(self.vision, vision)
        self.bestmidi.append(row(
            "Vision Assistant Pro", self.vision, "2026.10.22", channel="beta",
            last=(2026, 3, 0),
        ))
        self.official.append(row(
            "VisionAssistant", self.vision, "2026.10.22", source="official",
            last=(2026, 3, 0), sha256=hashlib.sha256(vision).hexdigest(),
        ))

        # Hashed before names were captured: answers 304 for ever.
        self.stuck = BASE + "stuck.nvda-addon"
        self._seeded("Stuck Display Name", self.stuck, manifest_text("stuckAddon"))
        # Hashed by a build that wrote the name as known-empty on a 304.
        self.empty = BASE + "empty.nvda-addon"
        self._seeded("Empty Display Name", self.empty, manifest_text("emptyAddon"),
                     manifest_name="")
        # Name known from an earlier build, API versions not yet captured.
        self.known = BASE + "known.nvda-addon"
        self._seeded("knownAddon", self.known,
                     manifest_text("knownAddon", last="2025.1"),
                     last=(2027, 1, 0), manifest_name="knownAddon")
        # A host that ignores Range and would send the whole file.
        self.refuser = BASE + "refuser.nvda-addon"
        self._seeded("Refuser Display", self.refuser, manifest_text("refuserAddon"),
                     ranges=False)
        # Recheck not due for weeks: the wrong ID must not wait for it.
        self.waiting = BASE + "waiting.nvda-addon"
        self._seeded("Waiting Display", self.waiting, manifest_text("waitingAddon"),
                     next_check=T0 + 999 * HOUR)
        self.down = BASE + "down.nvda-addon"
        self._seeded("Down Display", self.down, manifest_text("downAddon"),
                     next_check=T0 + 999 * HOUR)
        self.internet.files[self.down]["down"] = True

        # What the catalog states against what the bundle says.
        self.lying_version = BASE + "a11yTaskRecorder-0.2.0.nvda-addon"
        self._fresh("A11y Task Recorder", self.lying_version,
                    manifest_text("a11yTaskRecorder", "0.2.0"), version="0.5.11")
        self.colon = BASE + "VSCode.nvda-addon"
        self._fresh("VSCode", self.colon,
                    manifest_text("VSCode", "20.05.04:341d96a"),
                    version="20.05.04:341d96a")
        self.sha_version = BASE + "sha.nvda-addon"
        self._fresh("shaVersion", self.sha_version,
                    manifest_text("shaVersion", "ef7b3f4c0c03d6eb"), version="53.0.0")
        self.overclaim = BASE + "overclaim.nvda-addon"
        self._fresh("overclaimAddon", self.overclaim,
                    manifest_text("overclaimAddon", last="2025.1"), last=(2027, 1, 0))
        self.underclaim = BASE + "underclaim.nvda-addon"
        self._fresh("underclaimAddon", self.underclaim,
                    manifest_text("underclaimAddon", last="2027.1"), last=(2025, 3, 0))
        self.blank_api = BASE + "blank.nvda-addon"
        self._fresh("blankApiAddon", self.blank_api,
                    manifest_text("blankApiAddon", min_nvda=None, last=None),
                    last=(2027, 1, 0))
        self.future = BASE + "future.nvda-addon"
        self._fresh("futureAddon", self.future,
                    manifest_text("futureAddon", min_nvda="2099.1", last="2099.1"))
        self.quoted = BASE + "quoted.nvda-addon"
        self._fresh("Quoted Display", self.quoted,
                    manifest_text("'quotedAddon'"))
        self.commented = BASE + "commented.nvda-addon"
        self._fresh("Commented Display", self.commented,
                    manifest_text("commentedAddon # the id"))

        # A landing page where the file used to be.
        self.landing = BASE + "landing.nvda-addon"
        serve(self.landing, b"<!doctype html><title>Moved</title>", etag=None)
        self.bestmidi.append(row("Landing Page", self.landing))
        self.nameless = BASE + "nameless.nvda-addon"
        serve(self.nameless, bundle("version = 1.0\n"))
        self.bestmidi.append(row("Nameless Bundle", self.nameless))

        # The same file through two catalogs, under two IDs.
        self.twice = BASE + "twice.nvda-addon"
        serve(self.twice, bundle(manifest_text("twiceAddon")))
        self.bestmidi.append(row("Twice Listed", self.twice))
        self.ru.append(row("twiceAddon", self.twice, source="ru"))

        # A hand translation keyed by the ID the row used to have.
        self.translated = BASE + "translated.nvda-addon"
        self._fresh("Nombre Viejo", self.translated, manifest_text("nombreViejo"))

        # Blocked under its catalog name, manifest name already cached.
        self.blocked = BASE + "blocked.nvda-addon"
        self.bestmidi.append(row("Hacking by addon", self.blocked))
        self.seed[self.blocked] = {
            "sha256": "b" * 64, "size": 10, "version": "1.0",
            "next_check": T0 + 999 * HOUR, "manifest_name": "harmlessLooking",
            "manifest_last_tested": "2027.1",
        }

        # A catalog that tells the truth stays exactly as it is.
        self.honest = BASE + "honest.nvda-addon"
        serve(self.honest, bundle(manifest_text("honestAddon", "3.2.1")))
        self.ru.append(row("honestAddon", self.honest, "3.2.1", source="ru"))
        # A template name is not an identity, so the catalog's ID stands.
        self.template = BASE + "template.nvda-addon"
        serve(self.template, bundle(manifest_text("addonTemplate")))
        self.bestmidi.append(row("Template User", self.template))

    def _fresh(self, name, url, text, **fields):
        self.internet.serve(url, bundle(text))
        self.bestmidi.append(row(name, url, **fields))

    def _seeded(self, name, url, text, ranges=True, last=(2027, 1, 0), **cached):
        raw = bundle(text)
        self.internet.serve(url, raw, ranges=ranges)
        self.bestmidi.append(row(name, url, last=last))
        self.seed[url] = dict({
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
            "etag": '"1"', "version": "1.0", "next_check": T0 - 1,
            "manifest_version": "", "manifest_locales": {}, "manifest_summary": "",
        }, **cached)


class Build:
    """What one finished build left behind."""

    def __init__(self, out, internet, first_call):
        self.out = out
        self.calls_from = first_call
        self.calls_to = len(internet.calls)
        self.internet = internet

    def text(self, *parts):
        with open(os.path.join(self.out, *parts), encoding="utf-8") as f:
            return f.read()

    def rows(self, lang="en", ver="latest"):
        return nvda_contract.parse_store_rows(self.text(lang, "all", f"{ver}.json"))

    def by_id(self):
        return {r["addonId"]: r for r in self.rows()}

    def by_url(self, url):
        return [r for r in self.rows() if r["URL"] == url]

    def kinds(self):
        return [k for _, k in self.internet.calls[self.calls_from:self.calls_to]]

    def cached(self, url):
        return json.loads(self.text("hashcache.json"))[url]


def run_build(scenario, directory, now):
    out = os.path.join(directory, "public")
    argv = [
        "mirror.py", "--out", out, "--sources", ",".join(mirror.ALL_SOURCES),
        "--locales", "en,de", "--api-versions", ",".join(API_VERSIONS) + ",latest",
        "--hashcache", "hashcache.json", "--workers", "3", "--no-translate",
        "--site-base-url", SITE,
    ]
    first_call = len(scenario.internet.calls)
    with open(os.path.join(REPO, mirror.NVDA_API_VERSIONS_PATH), encoding="utf-8") as f:
        api_entries = json.load(f)
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(mirror.time, "time", lambda: now), \
            mock.patch.object(mirror, "urlopen", scenario.internet), \
            mock.patch.object(mirror, "MANIFEST_CAPTURE_LIMIT", CAPTURE_LIMIT), \
            mock.patch.object(mirror, "fetch_official",
                              lambda: copy.deepcopy(scenario.official)), \
            mock.patch.object(mirror, "fetch_bestmidi",
                              lambda: copy.deepcopy(scenario.bestmidi)), \
            mock.patch.object(mirror, "fetch_ru",
                              lambda: copy.deepcopy(scenario.ru)), \
            mock.patch.object(mirror, "fetch_es",
                              lambda: copy.deepcopy(scenario.es)), \
            mock.patch.object(mirror, "fetch_github_owners",
                              lambda existing_entries=None: copy.deepcopy(scenario.owners)), \
            mock.patch.object(mirror, "fetch_pinned",
                              lambda: copy.deepcopy(scenario.pinned)), \
            mock.patch.object(mirror, "load_nvda_api_version_entries",
                              lambda *a, **k: copy.deepcopy(api_entries)), \
            mock.patch.object(mirror, "back_compat_to_version",
                              lambda *a, **k: (2027, 1, 0)), \
            contextlib.redirect_stdout(io.StringIO()):
        mirror.main()
    return Build(out, scenario.internet, first_call)


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = Scenario()
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.addClassCleanup(os.chdir, os.getcwd())
        saved = (mirror.TRANSLATIONS, mirror.AUTO_ONLY_FIELDS, mirror.ENGLISH_CHANGELOGS)

        def restore():
            mirror.TRANSLATIONS, mirror.AUTO_ONLY_FIELDS, mirror.ENGLISH_CHANGELOGS = saved
        cls.addClassCleanup(restore)
        # main() reads and writes its caches beside itself.
        os.chdir(cls.directory.name)
        shutil.copy(os.path.join(REPO, mirror.NVDA_API_VERSIONS_PATH), ".")
        with open("hashcache.json", "w", encoding="utf-8") as f:
            json.dump(cls.scenario.seed, f)
        with open("translations.json", "w", encoding="utf-8") as f:
            json.dump({"translations": {"Nombre Viejo": {"summary": "Old Name"}}}, f)
        with open("blocked.json", "w", encoding="utf-8") as f:
            json.dump({"blocked": [{"name": "Hacking by addon", "reason": "malware"}]}, f)

        cls.first = run_build(cls.scenario, cls.directory.name, T0)
        cls.first_hash = cls.first.text("cacheHash.json")
        cls.second = run_build(cls.scenario, cls.directory.name, T0 + HOUR)
        cls.second_hash = cls.second.text("cacheHash.json")
        cls.second_rows = cls.second.rows()
        # Past every 24-hour recheck.
        cls.third = run_build(cls.scenario, cls.directory.name, T0 + 25 * HOUR)
        cls.third_hash = cls.third.text("cacheHash.json")

    def setUp(self):
        self.s = self.scenario
        self.ids = self.third.by_id()

    # ---- the store loads ----------------------------------------------

    def test_every_published_file_loads_in_nvda(self):
        for lang in ("en", "de"):
            for ver in API_VERSIONS + ("latest",):
                with self.subTest(lang=lang, ver=ver):
                    self.assertTrue(self.third.rows(lang, ver))

    def test_the_build_gate_accepts_the_output(self):
        self.assertEqual([], mirror.audit_catalog(self.third.out))

    def test_no_build_touched_an_unknown_url(self):
        self.assertEqual([], self.s.internet.unexpected)

    # ---- every offered install works ----------------------------------

    def test_every_offered_install_succeeds_on_every_nvda(self):
        # Known limits, each asserted on its own below: a host refusing ranges
        # gives no name to correct, and a template name is not an identity.
        limits = {self.s.refuser, self.s.template}
        checked = 0
        for ver in API_VERSIONS:
            for published in self.third.rows("en", "latest"):
                if published["URL"] in limits:
                    continue
                action = nvda_contract.store_action(
                    published, API_TUPLE[ver], BACK_COMPAT[ver])
                if action == "unavailable":
                    continue
                with self.subTest(addon=published["addonId"], ver=ver, action=action):
                    nvda_contract.install_from_store(
                        published, self.s.internet.files[published["URL"]]["raw"],
                        API_TUPLE[ver], BACK_COMPAT[ver],
                    )
                    checked += 1
        self.assertGreater(checked, 30)

    def test_a_compatible_list_never_needs_an_override(self):
        for ver in API_VERSIONS:
            for published in self.third.rows("en", ver):
                if published["URL"] == self.s.refuser:
                    continue
                with self.subTest(addon=published["addonId"], ver=ver):
                    self.assertEqual("install", nvda_contract.store_action(
                        published, API_TUPLE[ver], BACK_COMPAT[ver]))
                    nvda_contract.install_from_store(
                        published, self.s.internet.files[published["URL"]]["raw"],
                        API_TUPLE[ver], BACK_COMPAT[ver],
                    )

    # ---- every source -------------------------------------------------

    def test_every_source_reaches_the_catalog(self):
        labels = {r["storeSource"] for r in self.third.rows()}
        for source in mirror.ALL_SOURCES:
            with self.subTest(source=source):
                self.assertIn(mirror.STORE_SOURCE_LABELS[source], labels)

    def test_an_author_release_row_gets_its_real_id(self):
        self.assertEqual("ownerAddon", self.third.by_url(self.s.owner_url)[0]["addonId"])

    def test_a_pinned_bundle_is_hosted_byte_for_byte(self):
        published = self.ids["pinnedAddon"]
        self.assertEqual(self.s.pinned_url, published["URL"])
        with open(os.path.join(self.third.out, "downloads",
                               "pinnedAddon-2.0.nvda-addon"), "rb") as f:
            self.assertEqual(published["sha256"], hashlib.sha256(f.read()).hexdigest())

    # ---- the reported add-on ------------------------------------------

    def test_vision_assistant_has_one_row_under_its_real_id(self):
        self.assertNotIn("Vision Assistant Pro", self.ids)
        self.assertEqual(
            [("VisionAssistant", "stable", "2026.10.22")],
            [(r["addonId"], r["channel"], r["addonVersionName"])
             for r in self.third.by_url(self.s.vision)],
        )

    def test_vision_assistant_installs_with_the_override_on_2027_1(self):
        published = self.ids["VisionAssistant"]
        self.assertEqual("install-override", nvda_contract.store_action(
            published, (2027, 1, 0), (2027, 1, 0)))
        manifest = nvda_contract.install_from_store(
            published, self.s.internet.files[self.s.vision]["raw"],
            (2027, 1, 0), (2027, 1, 0),
        )
        self.assertEqual("VisionAssistant", manifest["name"])

    def test_the_wrong_id_is_what_nvda_refuses(self):
        # Proves the contract catches the original bug, not just its absence.
        published = dict(self.ids["VisionAssistant"], addonId="Vision Assistant Pro")
        with self.assertRaisesRegex(nvda_contract.StoreRefused, "not supported"):
            nvda_contract.install_from_store(
                published, self.s.internet.files[self.s.vision]["raw"],
                (2027, 1, 0), (2027, 1, 0),
            )

    # ---- catalog claims against the bundle ----------------------------

    def test_a_304_release_gets_its_real_id(self):
        self.assertEqual("stuckAddon", self.third.by_url(self.s.stuck)[0]["addonId"])

    def test_a_pinned_fork_and_its_original_are_both_listed(self):
        self.assertEqual(self.s.shared, self.ids["sharedAddon"]["URL"])
        self.assertEqual(self.s.fork_hosted, self.ids["forkOwner-sharedAddon"]["URL"])
        self.assertEqual(0, self.s.internet.count(self.s.fork_release, "full"))

    def test_a_wrong_id_is_corrected_in_the_next_build(self):
        self.assertEqual("waitingAddon",
                         self.first.by_url(self.s.waiting)[0]["addonId"])
        self.assertEqual(0, self.s.internet.count(self.s.waiting, "full"))
        self.assertEqual(0, self.s.internet.count(self.s.waiting, "304"))

    def test_a_failed_early_read_is_not_retried_every_build(self):
        self.assertEqual(1, self.s.internet.count(self.s.down, "down"))
        self.assertEqual("Down Display",
                         self.third.by_url(self.s.down)[0]["addonId"])

    def test_a_name_recorded_empty_gets_its_real_id(self):
        self.assertEqual("emptyAddon", self.third.by_url(self.s.empty)[0]["addonId"])

    def test_a_known_name_still_gets_its_api_versions(self):
        self.assertEqual(
            {"major": 2025, "minor": 1, "patch": 0},
            self.third.by_url(self.s.known)[0]["lastTestedVersion"],
        )

    def test_quotes_and_comments_are_not_part_of_an_id(self):
        self.assertEqual("quotedAddon", self.third.by_url(self.s.quoted)[0]["addonId"])
        self.assertEqual("commentedAddon",
                         self.third.by_url(self.s.commented)[0]["addonId"])

    def test_a_lying_version_is_corrected(self):
        published = self.third.by_url(self.s.lying_version)[0]
        self.assertEqual("0.2.0", published["addonVersionName"])
        self.assertEqual({"major": 0, "minor": 2, "patch": 0},
                         published["addonVersionNumber"])

    def test_a_version_nvda_cannot_parse_keeps_the_catalog_number(self):
        published = self.third.by_url(self.s.sha_version)[0]
        self.assertEqual("53.0.0", published["addonVersionName"])

    def test_a_colon_in_a_version_cannot_reach_a_file_name(self):
        self.assertEqual("20.05.04-341d96a",
                         self.third.by_url(self.s.colon)[0]["addonVersionName"])

    def test_an_overclaiming_row_leaves_the_compatible_list(self):
        ids = {r["addonId"] for r in self.third.rows("en", "2027.1.0")}
        self.assertNotIn("overclaimAddon", ids)
        self.assertNotIn("blankApiAddon", ids)

    def test_an_underclaiming_row_joins_the_compatible_list(self):
        ids = {r["addonId"] for r in self.third.rows("en", "2027.1.0")}
        self.assertIn("underclaimAddon", ids)

    def test_a_row_needing_a_newer_nvda_is_never_offered(self):
        published = self.ids["futureAddon"]
        for ver in API_VERSIONS:
            self.assertEqual("unavailable", nvda_contract.store_action(
                published, API_TUPLE[ver], BACK_COMPAT[ver]))

    def test_an_honest_row_is_untouched(self):
        published = self.ids["honestAddon"]
        self.assertEqual("3.2.1", published["addonVersionName"])
        self.assertEqual("NVDA Add-ons RU", published["storeSource"])

    def test_one_file_listed_twice_is_one_row(self):
        self.assertEqual(["twiceAddon"],
                         [r["addonId"] for r in self.third.by_url(self.s.twice)])

    # ---- things that must not be published ----------------------------

    def test_a_landing_page_is_rejected(self):
        self.assertEqual([], self.third.by_url(self.s.landing))
        rejected = json.loads(self.third.text("rejected.json"))
        self.assertIn("Landing Page", json.dumps(rejected))

    def test_a_bundle_without_a_name_is_rejected(self):
        self.assertEqual([], self.third.by_url(self.s.nameless))

    def test_a_blocked_add_on_stays_blocked_after_its_rename(self):
        self.assertEqual([], self.third.by_url(self.s.blocked))
        self.assertNotIn("harmlessLooking", self.ids)
        self.assertEqual(0, self.s.internet.count(self.s.blocked, "full"))

    # ---- what a rename must not lose ----------------------------------

    def test_a_translation_survives_the_rename(self):
        published = self.ids["nombreViejo"]
        self.assertEqual("Old Name", published["displayName"])

    def test_rows_with_no_answer_stay_published(self):
        self.assertEqual("Refuser Display",
                         self.third.by_url(self.s.refuser)[0]["addonId"])
        self.assertEqual("Template User",
                         self.third.by_url(self.s.template)[0]["addonId"])

    # ---- clients see the change ---------------------------------------

    def test_correcting_rows_moves_the_cache_hash(self):
        # NVDA refetches only when cacheHash.json changes.
        self.assertNotEqual(self.first_hash, self.second_hash)

    def test_a_settled_catalog_keeps_its_cache_hash(self):
        self.assertEqual(self.second_hash, self.third_hash)

    def test_a_settled_catalog_republishes_identical_rows(self):
        self.assertEqual(self.second_rows, self.third.rows())

    # ---- never re-download (AGENTS.md, the 2026-09-05 complaint) ------

    def test_a_build_inside_the_recheck_makes_no_package_request(self):
        self.assertEqual([], self.second.kinds())

    def test_a_recheck_downloads_nothing_a_validator_can_answer(self):
        # Only the landing page has no validator to answer with.
        fulls = [u for u, k in self.s.internet.calls[self.third.calls_from:self.third.calls_to]
                 if k == "full"]
        self.assertEqual({self.s.landing, self.s.nameless}, set(fulls))

    def test_a_recheck_reads_no_manifest_a_second_time(self):
        self.assertNotIn("range", self.third.kinds())

    def test_a_good_bundle_is_downloaded_once_across_all_builds(self):
        for url in (self.s.vision, self.s.lying_version, self.s.honest, self.s.twice):
            with self.subTest(url=url):
                self.assertLessEqual(self.s.internet.count(url, "full"), 1)

    def test_a_seeded_release_is_never_downloaded(self):
        for url in (self.s.stuck, self.s.empty, self.s.known, self.s.refuser):
            with self.subTest(url=url):
                self.assertEqual(0, self.s.internet.count(url, "full"))

    def test_a_manifest_read_costs_at_most_two_requests(self):
        for url in (self.s.stuck, self.s.vision):
            with self.subTest(url=url):
                self.assertIn(self.s.internet.count(url, "range"), (1, 2))

    def test_a_host_refusing_ranges_is_asked_once_ever(self):
        self.assertEqual(1, self.s.internet.count(self.s.refuser, "refused"))
        self.assertIsNone(self.third.cached(self.s.refuser)["manifest_name"])


if __name__ == "__main__":
    unittest.main()
