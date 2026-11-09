#!/usr/bin/env python3
"""Stamp deployed home/store pages with the GitHub-backfill notice.

The scheduled refresh builds the full catalog when it can. When the
github_owner source cannot verify (API quota), it publishes the other
sources instead of nothing; this notice on the two human pages says so.
A later full build overwrites the stamped files, removing the notice.

Usage: backfill_banner.py WEBROOT
"""

import pathlib
import sys

BANNER = (
    "<p><strong>Notice:</strong> GitHub author add-ons are still "
    "backfilling and appear automatically once verified.</p>"
)


def main(root):
    stamped = 0
    for name in ("index.html", "store.html"):
        path = pathlib.Path(root) / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "backfilling" in text:
            continue
        head, sep, tail = text.partition("</h1>")
        if not sep:
            continue
        path.write_text(head + sep + BANNER + tail, encoding="utf-8")
        stamped += 1
    print(f"stamped {stamped} page(s)")


if __name__ == "__main__":
    main(sys.argv[1])
