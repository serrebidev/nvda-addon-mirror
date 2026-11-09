import json
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "scripts")
)

import muse_translate


def finding(addon_id, **fields):
    return {
        "addonId": addon_id,
        "sourceURL": "https://example.invalid",
        "fields": {
            name: {"reason": "test", "text": text}
            for name, text in fields.items()
        },
    }


class NeededWorkTests(unittest.TestCase):
    def test_maps_display_name_to_summary_and_skips_done_fields(self):
        findings = [
            finding("newOne", displayName="Nom", description="Texte"),
            finding("halfDone", displayName="Nom", description="Texte"),
            finding("doneOne", displayName="Nom"),
        ]
        translations = {
            "halfDone": {"summary": "Half Done"},
            "doneOne": {"summary": "Done One"},
        }
        self.assertEqual(
            {
                "newOne": {"summary": "Nom", "description": "Texte"},
                "halfDone": {"description": "Texte"},
            },
            muse_translate.needed_work(findings, translations),
        )

    def test_empty_when_nothing_missing(self):
        findings = [finding("doneOne", displayName="Nom")]
        translations = {"doneOne": {"summary": "Done One"}}
        self.assertEqual({}, muse_translate.needed_work(findings, translations))


class ParseReplyTests(unittest.TestCase):
    WORK = {
        "newOne": {"summary": "Nom", "description": "Texte"},
        "halfDone": {"description": "Texte"},
    }

    def test_valid_reply_passes_through(self):
        reply = json.dumps({
            "newOne": {"summary": "Name", "description": "Text"},
            "halfDone": {"description": "Text"},
        })
        self.assertEqual(
            {
                "newOne": {"summary": "Name", "description": "Text"},
                "halfDone": {"description": "Text"},
            },
            muse_translate.parse_reply(reply, self.WORK),
        )

    def test_rejects_junk_unqueued_unrequested_and_empty(self):
        good = {
            "newOne": {"summary": "Name", "description": "Text"},
            "halfDone": {"description": "Text"},
        }
        bad_replies = [
            "not json",
            "[]",
            json.dumps({**good, "stranger": {"summary": "X"}}),
            json.dumps({
                **good, "halfDone": {"summary": "Sneaky", "description": "T"},
            }),
            json.dumps({**good, "newOne": {"motto": "X", "description": "T"}}),
            json.dumps({**good, "newOne": {"summary": " ", "description": "T"}}),
        ]
        for reply in bad_replies:
            with self.assertRaises(ValueError, msg=reply[:60]):
                muse_translate.parse_reply(reply, self.WORK)

    def test_partial_replies_apply_what_they_cover(self):
        reply = json.dumps({"newOne": {"summary": "Name"}})
        self.assertEqual(
            {"newOne": {"summary": "Name"}},
            muse_translate.parse_reply(reply, self.WORK),
        )


class ApplyTranslationsTests(unittest.TestCase):
    def test_fills_missing_only_and_appends_new_ids(self):
        data = {"translations": {
            "halfDone": {"description": "Kept text"},
        }}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "translations.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            added = muse_translate.apply_translations(path, {
                "halfDone": {"summary": "Half Done", "description": "Sneaky"},
                "brandNew": {"summary": "Brand New"},
            })
            with open(path, encoding="utf-8") as handle:
                written = json.load(handle)["translations"]

        self.assertEqual(["halfDone", "brandNew"], added)
        # The existing description is untouched; the sneaky overwrite lost.
        self.assertEqual(
            {"summary": "Half Done", "description": "Kept text"},
            written["halfDone"],
        )
        self.assertEqual({"summary": "Brand New"}, written["brandNew"])
        # New ids append at the end, in reply order.
        self.assertEqual(["halfDone", "brandNew"], list(written))


if __name__ == "__main__":
    unittest.main()
