#!/usr/bin/env python3
"""PreToolUse hook: warn an orchestrated session nearing its token_budget.

Registered in session-settings.json, which run.sh passes to every session it
launches, so it never fires on the owner's interactive work. run.sh exports the
task's raw `token_budget:` frontmatter value (`150k`, `2m`, `40000`) as
ORCH_TOKEN_BUDGET; a session launched without one makes this hook a no-op.

Unit: tokens, decided 2026-09-17. The frontmatter already speaks tokens, so the
budget is only parsed, never converted; USD stays in the ledger. Spend is the
latest assistant turn's context size (input + cache read + cache creation)
plus the output tokens summed over the whole transcript.

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
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", raw or "")
    if not m:
        return None
    value = float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]
    return int(value) if value > 0 else None


def spend(transcript):
    context, output = None, 0
    seen = set()
    with open(transcript, errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
                msg = rec.get("message") or {}
                usage = msg.get("usage")
            except (ValueError, AttributeError):
                continue
            if rec.get("type") != "assistant" or not isinstance(usage, dict):
                continue
            context = sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens"))
            # One API message is split into several records sharing its id;
            # count its output once.
            key = msg.get("id") or id(rec)
            if key not in seen:
                seen.add(key)
                output += int(usage.get("output_tokens") or 0)
    return None if context is None else context + output


def bucket_file(session_id):
    base = Path(os.environ.get(STATE_DIR_ENV) or tempfile.gettempdir())
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return base / f"orch-token-budget.{safe}"


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
    state = bucket_file(data.get("session_id"))
    try:
        last = int(state.read_text())
    except (OSError, ValueError):
        last = -1
    if bucket <= last:
        return
    state.write_text(str(bucket))
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
