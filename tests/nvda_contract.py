"""What NVDA does with a published row, independent of mirror.py.

Copied from nvaccess/nvda source so a test can follow a row all the way to an
installed add-on. Deliberately shares no parsing with mirror.py: a bug there
must not be able to hide itself here.

- parse_store_rows: addonStore/models/addon.py _createStoreCollectionFromJson.
  One bad row raises, and that empties the whole Add-on Store.
- store_action: addonStore/models/status.py with addonVersionCheck.py.
- install_from_store: addonStore/network.py _download and _checkChecksum, then
  addonStore/install.py _getAddonBundleToInstallIfValid.
"""
import hashlib
import io
import json
import re
import zipfile

CHANNELS = {"stable", "beta", "dev", "external"}
ILLEGAL_IN_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
REQUIRED_STRINGS = (
    "addonId", "displayName", "description", "publisher", "addonVersionName",
    "license", "sourceURL", "URL", "sha256",
)
VERSION_FIELDS = ("addonVersionNumber", "minNVDAVersion", "lastTestedVersion")


class StoreRefused(Exception):
    """NVDA's store could not load the row, or could not install it."""


def _version(row, field):
    value = row[field]
    if not isinstance(value, dict) or set(value) - {"major", "minor", "patch"}:
        raise StoreRefused(f"{field} is not MajorMinorPatch(**dict): {value!r}")
    if not isinstance(value.get("major"), int) or not isinstance(value.get("minor"), int):
        raise StoreRefused(f"{field} lacks major/minor ints: {value!r}")
    return (value["major"], value["minor"], value.get("patch", 0))


def parse_store_rows(raw):
    """Load a published file the way NVDA does; raises on anything it cannot."""
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise StoreRefused("catalog is not a JSON list")
    seen = set()
    for row in rows:
        for field in REQUIRED_STRINGS:
            if not isinstance(row.get(field), str):
                raise StoreRefused(f"{row.get('addonId')}: {field} is {row.get(field)!r}")
        if row.get("channel") not in CHANNELS:
            raise StoreRefused(f"{row['addonId']}: channel {row.get('channel')!r}")
        for field in VERSION_FIELDS:
            if field not in row:
                raise StoreRefused(f"{row['addonId']}: {field} missing")
            _version(row, field)
        if "scanResults" in row and not isinstance(row["scanResults"], dict):
            raise StoreRefused(f"{row['addonId']}: scanResults is {row['scanResults']!r}")
        # CaseInsensitiveDict per channel: a second row silently replaces the first.
        key = (row["addonId"].casefold(), row["channel"])
        if key in seen:
            raise StoreRefused(f"duplicate row {key}")
        seen.add(key)
    return rows


def _api(text):
    """addonAPIVersion.getAPIVersionTupleFromString; blank or None is 0.0.0."""
    value = (text or "").strip()
    if not value or value == "None":
        return (0, 0, 0)
    parts = value.split(".")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise StoreRefused(f"Failed to open add-on package: bad API version {text!r}")
    return (int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) == 3 else 0)


def read_manifest(raw):
    """AddonBundle's manifest, or the error NVDA reports opening the package."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            text = archive.read("manifest.ini").decode("utf-8-sig")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise StoreRefused(f"Failed to open add-on package: {exc}") from exc
    manifest = {}
    for line in text.splitlines():
        match = re.match(r"\s*(\w+)\s*=\s*(.*)$", line)
        if not match:
            continue
        value = match.group(2).strip()
        quoted = re.fullmatch(r"""(["'])(.*?)\1\s*(?:#.*)?""", value)
        manifest[match.group(1)] = (
            quoted.group(2) if quoted else value.split("#", 1)[0].strip()
        )
    for required in ("name", "summary", "author", "version"):
        if not manifest.get(required):
            raise StoreRefused(f"Failed to open add-on package: no {required}")
    return manifest


def _compatible(min_nvda, last_tested, api, back_compat):
    return min_nvda <= api and last_tested >= back_compat


def store_action(row, api, back_compat):
    """What the Add-on Store offers for this row, from the row alone."""
    min_nvda = _version(row, "minNVDAVersion")
    if min_nvda > api:
        return "unavailable"
    if _version(row, "lastTestedVersion") < back_compat:
        return "install-override"
    return "install"


def install_from_store(row, raw, api, back_compat):
    """Press the action the store offers. Returns the manifest, or raises."""
    action = store_action(row, api, back_compat)
    if action == "unavailable":
        raise StoreRefused("no install action is offered")
    # Installed add-on state is case-insensitive; the override is recorded
    # under the row's addonId before the download starts.
    overridden = {row["addonId"].casefold()} if action == "install-override" else set()

    file_name = f"{row['addonId']}-{row['addonVersionName']}.nvda-addon"
    if ILLEGAL_IN_FILE_NAME.search(file_name):
        raise StoreRefused(f"Unable to save add-on as a file: {file_name}")
    if hashlib.sha256(raw).hexdigest().casefold() != row["sha256"].casefold():
        raise StoreRefused("Add-on download not safe: checksum failed")

    manifest = read_manifest(raw)
    min_nvda = _api(manifest.get("minimumNVDAVersion"))
    last_tested = _api(manifest.get("lastTestedNVDAVersion"))
    if not _compatible(min_nvda, last_tested, api, back_compat):
        can_override = min_nvda <= api and last_tested < back_compat
        if not (can_override and manifest["name"].casefold() in overridden):
            raise StoreRefused(f"Add-on not supported {file_name}")
    return manifest
