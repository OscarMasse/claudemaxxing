#!/usr/bin/env python3
"""Per-session cost ledger: the memory the planner learns from.

Each account has its own ledger file at state/<account>/costs.jsonl, so
measured burn rates and cost stats never bleed across subscriptions.

  ledger.py record <state_dir> <result.json> <mode> <task> <model> <effort> \
                   <slice_min> <exit> <account> [est_tokens]
      Parse a `claude -p --output-format json` result, append one line to
      <state_dir>/costs.jsonl, and print the session's result text (for runs.out).

  ledger.py fields <result.json>
      Print "<cost_usd> <duration_min>" from a result file, for run.sh's
      mechanical digest journal line (needed before the result file is
      deleted). Missing/unparseable data reads as "0.0 0".

  Library: stats(state_dir) -> {task: {runs, cost_usd, out_tokens, total_tokens}}
           session_costs(state_dir) -> {(task, model): tokens_per_session}
           spent_since(state_dir, iso_ts) -> tokens burned since a timestamp
           accuracy(state_dir) -> per-task estimate-vs-actual error
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def record(state_dir, result_path, mode, task, model, effort, slice_min,
           exit_code, account, est_tokens=0):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": account,
        "mode": mode, "task": task, "model": model, "effort": effort,
        "slice_min": int(slice_min), "exit": int(exit_code),
        # What the gatekeeper predicted this slice would burn. Kept so the
        # estimate can be scored against the outcome (see accuracy()).
        "est_tokens": int(float(est_tokens or 0)),
    }
    text = ""
    try:
        data = json.loads(Path(result_path).read_text())
        entry["cost_usd"] = data.get("total_cost_usd")
        entry["num_turns"] = data.get("num_turns")
        entry["duration_ms"] = data.get("duration_ms")
        u = data.get("usage") or {}
        entry["input_tokens"] = u.get("input_tokens")
        entry["output_tokens"] = u.get("output_tokens")
        entry["cache_read"] = u.get("cache_read_input_tokens")
        entry["cache_write"] = u.get("cache_creation_input_tokens")
        text = data.get("result") or ""
    except (OSError, json.JSONDecodeError) as e:
        entry["parse_error"] = str(e)
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "costs.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
    return text


def result_fields(result_path):
    """(cost_usd, duration_min) from a `claude -p --output-format json` result
    file. Missing or unparseable data reads as (0.0, 0)."""
    try:
        data = json.loads(Path(result_path).read_text())
    except (OSError, json.JSONDecodeError):
        return 0.0, 0
    cost = data.get("total_cost_usd") or 0.0
    duration_ms = data.get("duration_ms") or 0
    return cost, round(duration_ms / 60000)


def stats(state_dir):
    out = {}
    path = Path(state_dir) / "costs.jsonl"
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = out.setdefault(e.get("task", "?"), {"runs": 0, "cost_usd": 0.0,
                                                "out_tokens": 0, "total_tokens": 0})
        t["runs"] += 1
        t["cost_usd"] += e.get("cost_usd") or 0.0
        t["out_tokens"] += e.get("output_tokens") or 0
        t["total_tokens"] += sum(e.get(k) or 0 for k in
                                 ("input_tokens", "output_tokens", "cache_read", "cache_write"))
    return out


def _entry_tokens(e):
    return sum(e.get(k) or 0 for k in
               ("input_tokens", "output_tokens", "cache_read", "cache_write"))


def _entries(state_dir):
    path = Path(state_dir) / "costs.jsonl"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def spent_since(state_dir, since):
    """Tokens burned by sessions started at or after `since` (a tz-aware
    datetime). Used to track how much of tonight's allocation is already gone,
    so the budget is a remainder rather than a fresh grant every tick."""
    total = 0
    for e in _entries(state_dir):
        ts = e.get("ts")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= since:
            total += _entry_tokens(e)
    return total


def accuracy(state_dir):
    """{task: {runs, est_tokens, actual_tokens, ratio}} for runs whose launch
    estimate was recorded. ratio > 1 means the planner under-estimated.

    This is the feedback loop on the burn estimates: `session_costs()` learns
    what a task costs per session, and this reports whether that learning is
    actually converging or systematically off for a given task."""
    out = {}
    for e in _entries(state_dir):
        est = e.get("est_tokens")
        if not est:
            continue
        actual = _entry_tokens(e)
        t = out.setdefault(e.get("task", "?"),
                           {"runs": 0, "est_tokens": 0, "actual_tokens": 0})
        t["runs"] += 1
        t["est_tokens"] += est
        t["actual_tokens"] += actual
    for t in out.values():
        t["ratio"] = (t["actual_tokens"] / t["est_tokens"]) if t["est_tokens"] else None
    return out


RATE_WINDOW = 5  # runs; a task's cost drifts as it moves through its work


def session_costs(state_dir, window=RATE_WINDOW):
    """Measured cost {(task, model): tokens_per_session} from the ledger.

    This is how the planner learns real task costs over time. Only the last
    `window` runs per (task, model) count: a task's cost drifts as it moves
    from cheap survey slices to expensive implementation ones, and averaging
    over all history keeps quoting a figure the task has already outgrown.

    Per SESSION, not per minute. This used to divide by `slice_min` and the
    caller multiplied by the slice again, which cancelled back to a per-session
    figure - correct by accident, and only while every slice had the same
    length. Sessions do not fill their slice (measured median utilisation on
    this backlog: 3%), so a per-minute rate does not describe anything real:
    what a task costs is a property of the work, not of the time it was
    allotted. The cold-start default (`est_session_tokens`) is in the same
    unit, so the measured and unmeasured paths can no longer disagree by the
    length of a slice."""
    recent = {}
    for e in _entries(state_dir):
        total = _entry_tokens(e)
        if total <= 0:
            continue
        key = (e.get("task", "?"), e.get("model", "sonnet"))
        runs = recent.setdefault(key, [])
        runs.append(total)
        if len(runs) > window:
            runs.pop(0)
    return {k: sum(runs) / len(runs) for k, runs in recent.items()}


if __name__ == "__main__":
    if len(sys.argv) >= 11 and sys.argv[1] == "record":
        print(record(*sys.argv[2:12]))
    elif len(sys.argv) == 3 and sys.argv[1] == "fields":
        cost, minutes = result_fields(sys.argv[2])
        print(f"{cost} {minutes}")
    else:
        print("usage: ledger.py record <state_dir> <json> <mode> <task> <model> "
              "<effort> <slice> <exit> <account> [est_tokens]\n"
              "       ledger.py fields <result.json>", file=sys.stderr)
        sys.exit(2)
