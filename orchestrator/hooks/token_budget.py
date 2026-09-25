#!/usr/bin/env python3
"""PreToolUse hook: warn an orchestrated session nearing its token_budget.

Registered in session-settings.json, which run.sh passes to every session it
launches, so it never fires on the owner's interactive work. run.sh exports the
task's raw `token_budget:` frontmatter value (`150k`, `2m`, `40000`) as
ORCH_TOKEN_BUDGET; a session launched without one makes this hook a no-op.

Unit: tokens, decided 2026-09-17. The frontmatter already speaks tokens, so the
budget is only parsed, never converted; USD stays in the ledger. Spend is the
latest assistant turn's context size (input + cache read + cache creation)
plus that turn's output: earlier outputs are already inside the context.
Known limit: subagent transcripts live in separate files and are not counted.

The hook never blocks a tool call and exits 0 on every path: a broken hook must
never be able to kill a night, and a false stop is worse than an overspend.
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

WARN_FRACTION = 0.8
# Once past WARN_FRACTION, warn again each time spend grows by this fraction
# of the budget, so a long session is nudged again rather than once.
REWARN_STEP = 0.2
STATE_DIR_ENV = "ORCH_HOOK_STATE_DIR"


def parse_budget(raw):
    raw = (raw or "").split("#", 1)[0].strip().strip("'\"")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmM]?)", raw)
    if not m:
        return None
    value = float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]
    return int(value) if value > 0 else None


def spend(transcript):
    last = None
    with open(transcript, errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
                usage = (rec.get("message") or {}).get("usage")
            except (ValueError, AttributeError):
                continue
            if rec.get("type") == "assistant" and isinstance(usage, dict):
                last = usage
    if last is None:
        return None
    return sum(int(last.get(k) or 0) for k in (
        "input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens"))


def marker(session_id, bucket):
    base = Path(os.environ.get(STATE_DIR_ENV) or tempfile.gettempdir())
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return base / f"orch-token-budget.{safe}.{bucket}"


def main():
    budget = parse_budget(os.environ.get("ORCH_TOKEN_BUDGET"))
    if budget is None:
        return
    data = json.load(sys.stdin)
    used = spend(data["transcript_path"])
    if used is None:
        return
    fraction = used / budget
    if fraction < WARN_FRACTION:
        return
    bucket = int((fraction - WARN_FRACTION) // REWARN_STEP)
    # One marker per (session, bucket), created atomically: PreToolUse hooks
    # of parallel tool calls run concurrently, and only the first may warn.
    # Reaching a bucket also claims the ones below it, so falling back (a
    # compaction shrinks the context) never re-warns.
    for b in range(bucket, -1, -1):
        try:
            os.close(os.open(marker(data.get("session_id"), b),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            if b == bucket:
                return
            break
    message = (
        f"TOKEN BUDGET WARNING: this session has used about {used:,} tokens, "
        f"{fraction:.0%} of its declared token_budget of {budget:,}. "
        "Stop now per step 7: finish the current edit, write a precise resume "
        "point in the task's ## Notes, set its status, commit, and exit.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "additionalContext": message}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
