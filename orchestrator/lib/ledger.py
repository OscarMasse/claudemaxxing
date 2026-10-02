#!/usr/bin/env python3
"""Per-session cost ledger: the memory the planner learns from.

Each account has its own ledger file at state/<account>/costs.jsonl, so
measured burn rates and cost stats never bleed across subscriptions.

  ledger.py record <state_dir> <result.json> <mode> <task> <model> <effort> \
                   <slice_min> <exit> <account> [est_usd]
      Parse a `claude -p --output-format stream-json --verbose` output (its
      final `{"type": "result"}` line; a single `--output-format json`
      object reads the same), append one line to
      <state_dir>/costs.jsonl, and print the session's result text (for runs.out).

  ledger.py compare <state_root> <engine-a> <engine-b>
      Per task and overall: run count, median USD and duration for each engine,
      over state/costs.jsonl and state/<account>/costs.jsonl. Descriptive only.

  ledger.py engines <state_root> <since-iso>
      Distinct engine versions of rows since a timestamp (digest line).

  ledger.py fields <result.json>
      Print "<cost_usd> <duration_min>" from a result file, for run.sh's
      mechanical digest journal line (needed before the result file is
      deleted). Missing/unparseable data reads as "0.0 0".

  Library: stats(state_dir) -> {task: {runs, cost_usd, out_tokens, total_tokens}}
           session_costs(state_dir) -> {(task, model): usd_per_session}
           spent_since(state_dir, ts) -> USD burned since a timestamp
           accuracy(state_dir) -> per-task estimate-vs-actual error, in USD
           outcome_histogram(runs_log) -> {outcome: count} over agent lines

Row schema. Every row carries REQUIRED_KEYS. A row whose result file could
not be parsed also carries `parse_error` (the exception text) and `reason`
("unreadable" or "invalid_json"); `exit` is the raw exit status of the run,
so the failure is diagnosable. Other keys (cost_usd, tokens, ...) are optional
and readers key off presence; historical rows are never backfilled.

The unit is USD: a session's actual cost is the entry's `cost_usd`, which is
Claude Code's own `total_cost_usd` for the run. That figure includes the
subagents the session spawned, which the top-level `usage` block does not
(measured 2026-09-12: pricing the top-level block alone came out 1.1x to 2.9x
under `total_cost_usd`), so it is the one to learn from. The token columns
stay for the digest's volume reporting only.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_KEYS = ("ts", "account", "mode", "task", "model", "effort",
                 "slice_min", "exit", "est_usd")


def load_result(result_path):
    """The session's result object from a run.sh output file.

    run.sh launches with `--output-format stream-json --verbose` so the
    `rate_limit_event` lines survive (lib/ratelimits.py); the final
    `{"type": "result"}` line carries exactly the object `--output-format
    json` used to print alone, so the ledger fields are unchanged. A file
    holding that single object (older runs, tests) still reads.
    Raises OSError or json.JSONDecodeError as json.loads would.
    """
    text = Path(result_path).read_text()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    result = None
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            result = obj
    if result is None:
        raise json.JSONDecodeError("no result line", text, 0)
    return result


def record(state_dir, result_path, mode, task, model, effort, slice_min,
           exit_code, account, est_usd=0, engine="", prompt_sha=""):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": account,
        "mode": mode, "task": task, "model": model, "effort": effort,
        "slice_min": int(slice_min), "exit": int(exit_code),
        # What the gatekeeper predicted this session would cost, in USD. Kept
        # so the estimate can be scored against the outcome (see accuracy()).
        "est_usd": float(est_usd or 0),
        # Which engine produced the row: short SHA (+ "-dirty") and a hash of
        # the rendered prompt. Rows before 2026-10-02 lack both: read as "unknown".
        "engine": engine or "unknown", "prompt_sha": prompt_sha or "unknown",
    }
    text = ""
    try:
        data = load_result(result_path)
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
        entry["reason"] = ("invalid_json" if isinstance(e, json.JSONDecodeError)
                           else "unreadable")
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "costs.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
    return text


def result_fields(result_path):
    """(cost_usd, duration_min) from a run.sh result file (load_result).
    Missing or unparseable data reads as (0.0, 0)."""
    try:
        data = load_result(result_path)
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


def no_cost_share(state_dir, last=50):
    """(no_cost_rows, total_rows) over the last `last` ledger rows: rows with
    no cost_usd, e.g. a parse failure. A rising share means ledger coverage
    is regressing and the gatekeeper is under-counting spend."""
    rows = list(_entries(state_dir))[-last:]
    return sum(1 for e in rows if e.get("cost_usd") is None), len(rows)


def _entry_usd(e):
    """The session's actual cost: Claude Code's `total_cost_usd`, subagents
    included. An entry without it (a failed run, or one recorded before the
    field existed) costs nothing here but still counts as a run in stats()."""
    return float(e.get("cost_usd") or 0.0)


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
    """USD burned by sessions started at or after `since` (a tz-aware
    datetime). Used to track how much of tonight's allocation is already gone,
    so the budget is a remainder rather than a fresh grant every tick."""
    total = 0.0
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
            total += _entry_usd(e)
    return total


def accuracy(state_dir):
    """{task: {runs, est_usd, actual_usd, ratio}} for runs whose launch
    estimate was recorded. ratio > 1 means the planner under-estimated.

    This is the feedback loop on the burn estimates: `session_costs()` learns
    what a task costs per session, and this reports whether that learning is
    actually converging or systematically off for a given task."""
    out = {}
    for e in _entries(state_dir):
        est = e.get("est_usd")
        if not est:
            continue
        actual = _entry_usd(e)
        t = out.setdefault(e.get("task", "?"),
                           {"runs": 0, "est_usd": 0.0, "actual_usd": 0.0})
        t["runs"] += 1
        t["est_usd"] += est
        t["actual_usd"] += actual
    for t in out.values():
        t["ratio"] = (t["actual_usd"] / t["est_usd"]) if t["est_usd"] else None
    return out


RATE_WINDOW = 5  # runs; a task's cost drifts as it moves through its work


def session_costs(state_dir, window=RATE_WINDOW):
    """Measured cost {(task, model): usd_per_session} from the ledger.

    This is how the planner learns real task costs over time. Only the last
    `window` runs per (task, model) count: a task's cost drifts as it moves
    from cheap survey slices to expensive implementation ones, and averaging
    over all history keeps quoting a figure the task has already outgrown.

    The figure is the MAXIMUM of those runs, not their mean. Measured on
    2026-09-12 the actual/estimate ratio ranged from 0.1 to 15: a task
    estimated from a 1M-token opening survey slice then ran a 25-minute
    implementation slice, and the mean of the two kept quoting the survey.
    The asymmetry decides it: an under-estimate launches sessions the night
    cannot pay for, an over-estimate launches one fewer session and the next
    tick re-measures.

    Per SESSION, not per minute. This used to divide by `slice_min` and the
    caller multiplied by the slice again, which cancelled back to a per-session
    figure - correct by accident, and only while every slice had the same
    length. Sessions do not fill their slice (measured median utilisation on
    this backlog: 3%), so a per-minute rate does not describe anything real:
    what a task costs is a property of the work, not of the time it was
    allotted. The cold-start default (`est_session_usd`) is in the same
    unit, so the measured and unmeasured paths can no longer disagree by the
    length of a slice."""
    recent = {}
    for e in _entries(state_dir):
        cost = _entry_usd(e)
        if cost <= 0:
            continue
        key = (e.get("task", "?"), e.get("model", "sonnet"))
        runs = recent.setdefault(key, [])
        runs.append(cost)
        if len(runs) > window:
            runs.pop(0)
    return {k: max(runs) for k, runs in recent.items()}


def _all_rows(state_root):
    """Rows of state/costs.jsonl and every state/<account>/costs.jsonl."""
    root = Path(state_root)
    dirs = [root] + sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    for d in dirs:
        yield from _entries(d)


MIN_RUNS = 3  # per side; below this a median says nothing


def _median(values):
    values = sorted(values)
    n = len(values)
    if not n:
        return None
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def compare(state_root, engine_a, engine_b):
    """Descriptive comparison of two engine versions over real ledger rows.

    Returns {task_or_"ALL": {engine: {runs, median_usd, median_min}}}. Rows
    without an `engine` read as "unknown". Not a controlled experiment."""
    groups = {}
    for e in _all_rows(state_root):
        eng = e.get("engine") or "unknown"
        if eng not in (engine_a, engine_b):
            continue
        usd = _entry_usd(e)
        mins = (e.get("duration_ms") or 0) / 60000
        for key in (e.get("task", "?"), "ALL"):
            groups.setdefault(key, {}).setdefault(eng, []).append((usd, mins))
    out = {}
    for key, per in groups.items():
        out[key] = {}
        for eng in (engine_a, engine_b):
            rows = per.get(eng, [])
            out[key][eng] = {"runs": len(rows),
                             "median_usd": _median([r[0] for r in rows]),
                             "median_min": _median([r[1] for r in rows])}
    return out


def format_compare(result, engine_a, engine_b):
    lines = []
    for key in sorted(result, key=lambda k: (k != "ALL", k)):
        a, b = result[key][engine_a], result[key][engine_b]
        cells = []
        for eng, m in ((engine_a, a), (engine_b, b)):
            usd = "-" if m["median_usd"] is None else f"${m['median_usd']:.2f}"
            mins = "-" if m["median_min"] is None else f"{m['median_min']:.1f}min"
            cells.append(f"{eng}: runs={m['runs']} median={usd} {mins}")
        note = ""
        if min(a["runs"], b["runs"]) < MIN_RUNS:
            note = f"  (too few runs: need {MIN_RUNS} per side)"
        lines.append(f"{key}: " + " | ".join(cells) + note)
    return "\n".join(lines) or "no rows for either engine"


def engines_since(state_root, since):
    """Distinct engine versions of rows at or after `since` (tz-aware
    datetime), for the digest. Rows without one read as "unknown"."""
    seen = []
    for e in _all_rows(state_root):
        try:
            when = datetime.fromisoformat(e.get("ts", ""))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= since:
            eng = e.get("engine") or "unknown"
            if eng not in seen:
                seen.append(eng)
    return seen


# The controlled vocabulary of `stopped=` in the agent lines of runs.log
# (prompts/orchestrate.md step 9). There is no "killed": a session cut off by
# run.sh's timeout never reaches step 9, so only the launcher line records it.
OUTCOMES = ("done", "resumed", "routine-pass", "blocked", "noop")

# runs.log interleaves two formats. The agent writes `2026-09-30T02:26:23+02:00
# task=... did=... stopped=<outcome>`; the launcher writes `2026-09-30 01:58:34
# mode=... exit=0`. Only the agent's line has a `task=` right after the date.
_AGENT_LINE = re.compile(r"^\d{4}-\d\d-\d\d\S* task=")


def outcome_histogram(runs_log):
    """Count the agent lines of runs.log by outcome. Launcher lines are
    skipped; any `stopped=` value outside OUTCOMES (or missing) counts as
    `unknown`, which is where every pre-vocabulary line lands."""
    hist = {}
    path = Path(runs_log)
    if not path.exists():
        return hist
    for line in path.read_text().splitlines():
        if not _AGENT_LINE.match(line):
            continue
        _, sep, value = line.rpartition(" stopped=")
        value = value.strip() if sep else ""
        key = value if value in OUTCOMES else "unknown"
        hist[key] = hist.get(key, 0) + 1
    return hist


if __name__ == "__main__":
    if len(sys.argv) >= 11 and sys.argv[1] == "record":
        print(record(*sys.argv[2:14]))
    elif len(sys.argv) == 5 and sys.argv[1] == "compare":
        print(format_compare(compare(sys.argv[2], sys.argv[3], sys.argv[4]),
                             sys.argv[3], sys.argv[4]))
    elif len(sys.argv) == 4 and sys.argv[1] == "engines":
        print(" ".join(engines_since(sys.argv[2],
                                     datetime.fromisoformat(sys.argv[3]))) or "none")
    elif len(sys.argv) == 3 and sys.argv[1] == "fields":
        cost, minutes = result_fields(sys.argv[2])
        print(f"{cost} {minutes}")
    else:
        print("usage: ledger.py record <state_dir> <json> <mode> <task> <model> "
              "<effort> <slice> <exit> <account> [est_usd]\n"
              "       ledger.py compare <state_root> <engine-a> <engine-b>\n"
              "       ledger.py engines <state_root> <since-iso>\n"
              "       ledger.py fields <result.json>", file=sys.stderr)
        sys.exit(2)

