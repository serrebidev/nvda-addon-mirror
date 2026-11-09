import json
import os
import sys
import tempfile
import unittest
from unittest import mock

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


class SendMuseTests(unittest.TestCase):
    def test_first_run_starts_and_saves_the_side_chat(self):
        calls = []

        def fake_run(*args, timeout):
            calls.append(args)
            if args[0] == "session-start":
                return {"session_id": "chat-1"}
            return {"reply": {"text": "hello"}}

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                muse_translate, "_run_muse_cli", side_effect=fake_run
            ):
                self.assertEqual(
                    "hello", muse_translate.send_muse("hi", repo=directory)
                )
            with open(
                os.path.join(directory, muse_translate.THREAD_FILENAME),
                encoding="utf-8",
            ) as handle:
                self.assertEqual("chat-1\n", handle.read())
        self.assertEqual("session-start", calls[0][0])
        self.assertEqual(
            ("send", "--thread", "chat-1", "--wait", "600", "hi"), calls[1]
        )

    def test_later_runs_reuse_the_saved_chat(self):
        calls = []

        def fake_run(*args, timeout):
            calls.append(args)
            return {"reply": {"text": "hello"}}

        with tempfile.TemporaryDirectory() as directory:
            with open(
                os.path.join(directory, muse_translate.THREAD_FILENAME),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("chat-9\n")
            with mock.patch.object(
                muse_translate, "_run_muse_cli", side_effect=fake_run
            ):
                self.assertEqual(
                    "hello", muse_translate.send_muse("hi", repo=directory)
                )
        self.assertEqual(1, len(calls))
        self.assertEqual(
            ("send", "--thread", "chat-9", "--wait", "600", "hi"), calls[0]
        )

    def test_dead_chat_starts_a_new_one_and_retries(self):
        calls = []

        def fake_run(*args, timeout):
            calls.append(args)
            if args[:2] == ("send", "--thread") and args[2] == "chat-dead":
                raise RuntimeError("no such thread")
            if args[0] == "session-start":
                return {"session_id": "chat-2"}
            return {"reply": {"text": "hello"}}

        with tempfile.TemporaryDirectory() as directory:
            thread_path = os.path.join(
                directory, muse_translate.THREAD_FILENAME
            )
            with open(thread_path, "w", encoding="utf-8") as handle:
                handle.write("chat-dead\n")
            with mock.patch.object(
                muse_translate, "_run_muse_cli", side_effect=fake_run
            ):
                self.assertEqual(
                    "hello", muse_translate.send_muse("hi", repo=directory)
                )
            with open(thread_path, encoding="utf-8") as handle:
                self.assertEqual("chat-2\n", handle.read())
        self.assertEqual(
            [
                ("send", "--thread", "chat-dead", "--wait", "600", "hi"),
                ("session-start", "--title", "NVDA mirror translations"),
                ("send", "--thread", "chat-2", "--wait", "600", "hi"),
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
