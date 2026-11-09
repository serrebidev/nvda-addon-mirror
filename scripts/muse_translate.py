#!/usr/bin/env python3
"""Fill missing English translations via the owner's muse.ai agent.

Reads a findings file from ``audit_translations.py --json``, sends the items
whose ``translations.json`` entry lacks the flagged field to muse.ai, and
merges the answers back. Only missing fields are ever added; existing human
text is never changed. With ``--commit`` the result is committed and pushed
so the next scheduled build publishes it.

Report-only by design: any failure -- muse.ai unreachable, a reply that does
not validate, a push refused -- exits nonzero with a log line, and the
scheduled refresh treats that as non-blocking. Nothing is half-written: the
file is updated only after the whole reply validates.

    python scripts/muse_translate.py findings.json --commit
    python scripts/muse_translate.py findings.json --dry-run
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

TRANSLATIONS_PATH = "translations.json"

#: Upper bound per run. Leftovers stay queued for the next run; this caps
#: one muse.ai round-trip in time and cost.
MAX_ITEMS_PER_RUN = 40

#: muse.ai CLI. Overridable for tests.
MUSE_CLI = os.environ.get("MUSE_CLI") or shutil.which("muse-cli") or ""

RULES = (
    "Translate these NVDA add-on metadata items into English. "
    "Rules: translate INTO English only. For displayName give the English "
    "name first and the original after ', AKA ' (example: Descriptor, AKA "
    "Betimleyici). Keep key combos, version numbers and paths verbatim. "
    "Reply with ONLY a JSON object mapping each addonId to translated "
    "fields, using key summary for displayName and key description."
)


def needed_work(findings, translations):
    """Map addonId to {field: text} for fields missing English.

    Only fields absent from ``translations`` are wanted; anything already
    there -- human-written -- is left alone. ``displayName`` findings map to
    the ``summary`` key.
    """
    work = {}
    for finding in findings:
        addon_id = finding.get("addonId", "")
        entry = translations.get(addon_id, {})
        for field, detail in (finding.get("fields") or {}).items():
            key = "summary" if field == "displayName" else "description"
            if key not in entry:
                work.setdefault(addon_id, {})[key] = detail.get("text", "")
    return {addon_id: fields for addon_id, fields in work.items() if fields}


def build_message(work):
    """Render the muse.ai request for one batch of work."""
    parts = [RULES, " Items:"]
    index = 0
    for addon_id, fields in work.items():
        for key, text in fields.items():
            index += 1
            parts.append(f" {index}. {addon_id} {key}: {text}")
    return "".join(parts)


def _run_muse_cli(*args, timeout):
    """Run muse-cli, returning parsed JSON; raise RuntimeError on failure."""
    if not MUSE_CLI:
        raise RuntimeError("muse-cli is not installed")
    try:
        completed = subprocess.run(
            [MUSE_CLI, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"muse-cli {args[0]} failed: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"muse-cli {args[0]} exited {completed.returncode}: "
            f"{completed.stderr.strip()[:300]}"
        )
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"muse-cli {args[0]} returned unparsable output"
        ) from exc


def send_muse(message, wait=600):
    """Send one task to a fresh muse.ai side chat; return the reply text."""
    session = _run_muse_cli(
        "session-start", "--title", "NVDA mirror translations", timeout=120
    )
    session_id = session.get("session_id", "")
    if not session_id:
        raise RuntimeError("muse-cli session-start returned no session_id")
    answer = _run_muse_cli(
        "send", "--thread", session_id, "--wait", str(wait), message,
        timeout=wait + 60,
    )
    reply = (answer.get("reply") or {}).get("text", "")
    if not reply:
        raise RuntimeError(
            "muse.ai gave no reply in time; "
            f"check the side chat {session_id} manually"
        )
    return reply


def parse_reply(text, work):
    """Parse and strictly validate a muse.ai translation reply.

    Every key must be a queued add-on id, every field one of the wanted
    fields, every value a non-empty string. Anything else raises: a
    sloppy reply must never reach the translations file. Omissions are
    fine -- missing items simply stay queued for the next run.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"reply is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("reply is not a non-empty JSON object")
    merged = {}
    for addon_id, fields in data.items():
        if addon_id not in work:
            raise ValueError(f"reply covers unqueued add-on {addon_id!r}")
        if not isinstance(fields, dict) or not fields:
            raise ValueError(f"reply for {addon_id!r} is not a field object")
        for key, value in fields.items():
            if key not in ("summary", "description"):
                raise ValueError(f"reply has unknown field {key!r}")
            if key not in work[addon_id]:
                raise ValueError(
                    f"reply gives unrequested {key} for {addon_id!r}"
                )
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"reply for {addon_id!r}.{key} is empty")
            if len(value) > 4000:
                raise ValueError(f"reply for {addon_id!r}.{key} is too long")
        merged[addon_id] = dict(fields)
    return merged


def apply_translations(path, merged):
    """Fill missing fields from validated answers; return added ids.

    Existing fields are never touched. New ids append at the end
    (insertion order); ``summary`` sorts first to match the file's style.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    translations = data["translations"]
    added = []
    for addon_id, fields in merged.items():
        entry = translations.setdefault(addon_id, {})
        fresh = {k: v for k, v in fields.items() if k not in entry}
        if not fresh:
            continue
        combined = dict(entry)
        combined.update(fresh)
        ordered = {}
        if "summary" in combined:
            ordered["summary"] = combined.pop("summary")
        ordered.update(combined)
        translations[addon_id] = ordered
        added.append(addon_id)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return added


def commit_and_push(repo, message, token, host="git.serrebiradio.com",
                    slug="serrebi/nvda-addon-mirror"):
    """Commit translations.json and push; raise RuntimeError on failure."""
    def git(*args, **kwargs):
        completed = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            timeout=120, check=False, **kwargs,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: "
                f"{completed.stderr.strip()[:300]}"
            )
        return completed

    git("add", TRANSLATIONS_PATH)
    git("commit", "-m", message)
    header = f"Authorization: token {token}"
    git("-c", f"http.extraHeader={header}", "push",
        f"https://{host}/{slug}.git", "HEAD:main")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fill missing translations via muse.ai.")
    parser.add_argument("findings", help="audit_translations.py --json file")
    parser.add_argument("--repo", default=".",
                        help="repo checkout holding translations.json")
    parser.add_argument("--commit", action="store_true",
                        help="commit and push the result")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be sent, send nothing")
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS_PER_RUN)
    args = parser.parse_args(argv)

    with open(args.findings, encoding="utf-8") as handle:
        findings = json.load(handle)
    if not findings:
        print("translation queue is empty; nothing to do")
        return 0
    translations_path = os.path.join(args.repo, TRANSLATIONS_PATH)
    with open(translations_path, encoding="utf-8") as handle:
        translations = json.load(handle)["translations"]
    work = needed_work(findings, translations)
    if not work:
        print("every queued field already has English; nothing to do")
        return 0
    if len(work) > args.max_items:
        print(f"{len(work)} queued, capping this run at {args.max_items}")
        work = dict(list(work.items())[:args.max_items])
    if args.dry_run:
        print(f"would send {len(work)} add-ons: {', '.join(work)}")
        return 0
    reply = send_muse(build_message(work))
    merged = parse_reply(reply, work)
    uncovered = [i for i in work if set(work[i]) - set(merged.get(i, {}))]
    if uncovered:
        print(f"still queued for next run: {', '.join(uncovered)}")
    token = os.environ.get("FORGEJO_PUSH_TOKEN", "")
    if args.commit and not token:
        raise RuntimeError(
            "FORGEJO_PUSH_TOKEN is not set; refusing to leave "
            "uncommitted translations behind"
        )
    added = apply_translations(translations_path, merged)
    print(f"added English for {len(added)} add-ons: {', '.join(added)}")
    if args.commit:
        commit_and_push(
            args.repo,
            f"translations: English for {len(added)} queued add-ons "
            "(via muse.ai)",
            token,
        )
        print("committed and pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
