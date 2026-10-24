"""Published add-on IDs must be legal Windows file names.

NVDA's store client saves a download as "<addonId>-<version>.nvda-addon";
an ID carrying a character Windows forbids (<>:"/\\|?*) fails the final
rename with WinError 87, making the add-on uninstallable. Real cases below
come from the live catalog on 2026-10-23 (22 affected IDs, mostly from
BestMidi using the display name as the ID).
"""

import re
import unittest

import mirror

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class SanitizeAddonIdTests(unittest.TestCase):
    def test_real_catalog_ids_become_legal(self):
        cases = {
            "Access TV: Siaran TV Streaming Aksesibel":
                "Access TV- Siaran TV Streaming Aksesibel",
            "BOA: Better Office Accessibility":
                "BOA- Better Office Accessibility",
            "QBraille XL / Braille EDGE 點字顯示器驅動程式":
                "QBraille XL - Braille EDGE 點字顯示器驅動程式",
            "Notepad++ accessibility for Python and C/C++ development (32 and 64 bits)":
                "Notepad++ accessibility for Python and C-C++ development (32 and 64 bits)",
            "Thunderbird+ G5.1 (TB >= 140)":
                "Thunderbird+ G5.1 (TB -= 140)",
            'speechPlayerInEspeak""': "speechPlayerInEspeak-",
            "No Out of Spelling/Grammar Error":
                "No Out of Spelling-Grammar Error",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                cleaned = mirror.sanitize_addon_id(raw)
                self.assertEqual(cleaned, expected)
                self.assertIsNone(ILLEGAL.search(cleaned))

    def test_clean_ids_pass_through_unchanged(self):
        for name in ("weatherPlus", "Access8Math", "Auto Calc Avanzada y Liquidación",
                     "Hacking by addon"):
            self.assertEqual(mirror.sanitize_addon_id(name), name)

    def test_idempotent_and_never_empty(self):
        for raw in ("Access TV: Siaran TV Streaming Aksesibel", "a//b::c", "...", ""):
            once = mirror.sanitize_addon_id(raw)
            self.assertEqual(mirror.sanitize_addon_id(once), once)
        self.assertEqual(mirror.sanitize_addon_id("..."), "...")


if __name__ == "__main__":
    unittest.main()
