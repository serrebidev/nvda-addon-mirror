#!/usr/bin/env python3
"""Pack the helper add-on (helper/) into a .nvda-addon under dist/."""

import os
import re
import zipfile

NAME = "addonStoreMirror"
ADDON_DIR = "helper"
DIST_DIR = "dist"

with open(os.path.join(ADDON_DIR, "manifest.ini"), encoding="utf-8") as manifest_file:
    match = re.search(r"^version\s*=\s*([A-Za-z0-9._-]+)\s*$", manifest_file.read(), re.MULTILINE)
if not match:
    raise ValueError("The helper manifest must contain a safe version")
VERSION = match.group(1)

os.makedirs(DIST_DIR, exist_ok=True)
out = os.path.join(DIST_DIR, f"{NAME}-{VERSION}.nvda-addon")

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _dirs, files in os.walk(ADDON_DIR):
        _dirs[:] = [directory for directory in _dirs if directory != "__pycache__"]
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(root, f)
            arcname = os.path.relpath(path, ADDON_DIR)
            z.write(path, arcname)

print(f"built {out}")
