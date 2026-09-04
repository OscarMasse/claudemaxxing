#!/usr/bin/env python3
"""Per-session cost ledger: the memory the planner learns from.

Each account has its own ledger file at state/<account>/costs.jsonl, so
measured burn rates and cost stats never bleed across subscriptions.

  ledger.py record <state_dir> <result.json> <mode> <task> <model> <effort> \
                   <slice_min> <exit> <account>
      Parse a `claude -p --output-format json` result, append one line to
      <state_dir>/costs.jsonl, and print the session's result text (for runs.out).

  ledger.py fields <result.json>
      Print "<cost_usd> <duration_min>" from a result file, for run.sh's
      mechanical digest journal line (needed before the result file is
      deleted). Missing/unparseable data reads as "0.0 0".

  Library: stats(state_dir) -> {task: {runs, cost_usd, out_tokens, total_tokens}}
           rates(state_dir) -> {(task, model): tokens_per_min}
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def record(state_dir, result_path, mode, task, model, effort, slice_min,
           exit_code, account):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": account,
        "mode": mode, "task": task, "model": model, "effort": effort,
        "slice_min": int(slice_min), "exit": int(exit_code),
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


def rates(state_dir):
    """Measured burn rates {(task, model): tokens_per_min} from the ledger.
    This is how the planner learns real task costs over time."""
    acc = {}
    path = Path(state_dir) / "costs.jsonl"
    if not path.exists():
        return {}
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        mins = e.get("slice_min") or 0
        total = sum(e.get(k) or 0 for k in
                    ("input_tokens", "output_tokens", "cache_read", "cache_write"))
        if mins <= 0 or total <= 0:
            continue
        key = (e.get("task", "?"), e.get("model", "sonnet"))
        tot, m = acc.get(key, (0, 0))
        acc[key] = (tot + total, m + mins)
    return {k: tot / m for k, (tot, m) in acc.items()}


if __name__ == "__main__":
    if len(sys.argv) >= 11 and sys.argv[1] == "record":
        print(record(*sys.argv[2:11]))
    elif len(sys.argv) == 3 and sys.argv[1] == "fields":
        cost, minutes = result_fields(sys.argv[2])
        print(f"{cost} {minutes}")
    else:
        print("usage: ledger.py record <state_dir> <json> <mode> <task> <model> "
              "<effort> <slice> <exit> <account>\n"
              "       ledger.py fields <result.json>", file=sys.stderr)
        sys.exit(2)
