"""Is this task advancing? Stall, retry-storm and global launch-failure detection.

`repair_stuck` (lib/tasks.py) catches a session that died. This module catches
the opposite and more expensive failure: a task that runs, exits cleanly, goes
back to `ready`, and is relaunched having advanced nothing. The scheduler ranks
by priority and age only, so without this such a task keeps the front of the
queue night after night.

Inputs, all under the backlog's orchestrator/state/:
  runs.log                 run.sh's one line per session (`mode=... task=... exit=N`)
  launches.jsonl           one row per queue-task launch: the task file's hash
                           just before the claim (written by record_launch)
  costs.jsonl, <acct>/costs.jsonl
                           the ledger (cost and duration per session)
  detector.json            per task, when the detector last blocked it, so a
                           task the owner unblocks starts from a clean count

A detected task is written `blocked` with a note (decided 2026-09-17): written
state survives a restart and shows up in git, a silent skip does neither.
"""
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

STALL_RUNS = 4          # launches with unchanged content before a task is stalled
STORM_NIGHT_RUNS = 5    # launches within one night before a task is storming
STORM_SAME_EXIT = 3     # consecutive sessions on one task with one non-zero exit
# with_timeout.py's code when a slice runs out: a long task being cut off is
# working, not failing, and the stall check judges whether it advanced.
TIMEOUT_EXIT = 124
GLOBAL_FAIL_RUNS = 5    # consecutive zero-work failures, any task, before PAUSED

# A night is bucketed by the local date 12 hours earlier, so 22:00 and 04:00
# fall in the same night and a morning burn-down in the next one.
NIGHT_SHIFT = timedelta(hours=12)

_RUN_RE = re.compile(r"^(\S+ \S+) mode=(\S+) .*?task=(\S+) .* exit=(-?\d+)\s*$")
_SLICE_RE = re.compile(r" slice=(\d+)min ")
# Lines the gatekeeper itself adds to a task on every launch: they change the
# file without the session having advanced anything.
_NOISE_RE = re.compile(r"^(status:.*|- \S+: claimed by the gatekeeper at launch.*)$",
                       re.M)


def task_name(raw):
    """The one normalized form of a `task=` value: the file's basename.

    runs.log carries three historical spellings of the same task
    (`tasks/x.md`, `x.md`, `/Users/.../tasks/x.md`); counting over the raw
    field splits one task into three."""
    return Path(raw.strip()).name


def content_hash(path):
    """Hash of a task file minus the gatekeeper's own per-launch edits
    (status line, claim notes) and blank-line noise."""
    text = Path(path).read_text(errors="replace")
    lines = [l.rstrip() for l in _NOISE_RE.sub("", text).splitlines()]
    kept = "\n".join(l for l in lines if l)
    return hashlib.sha256(kept.encode()).hexdigest()


def _parse_ts(raw):
    try:
        return datetime.fromisoformat(raw.replace("T", " ", 1))
    except ValueError:
        return None


def orchestrate_runs(state_root):
    """run.sh's orchestrate lines, in file order: dicts with ts, task, exit."""
    path = Path(state_root) / "runs.log"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        m = _RUN_RE.match(line)
        if not m or m.group(2) != "orchestrate" or m.group(3) == "auto":
            continue
        ts = _parse_ts(m.group(1))
        if ts is None:
            continue
        s = _SLICE_RE.search(line)
        out.append({"ts": ts, "task": task_name(m.group(3)),
                    "exit": int(m.group(4)),
                    "slice": int(s.group(1)) if s else None})
    return out


def ledger_rows(state_root):
    """Ledger rows from state/costs.jsonl AND every state/<account>/costs.jsonl
    (rows exist at both levels), sorted by timestamp."""
    root = Path(state_root)
    rows = []
    for path in [root / "costs.jsonl", *sorted(root.glob("*/costs.jsonl"))]:
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("ts"):
                rows.append(row)
    # The same session can be recorded at both levels: count it once.
    unique = {(r["ts"], r.get("mode"), r.get("task")): r for r in rows}
    return sorted(unique.values(), key=lambda r: r["ts"])


def record_launch(state_root, path, ts):
    """Append the task file's pre-claim hash: the stall check compares these."""
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    row = {"ts": ts.isoformat(), "task": task_name(str(path)),
           "hash": content_hash(path)}
    with open(root / "launches.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def _launches(state_root):
    path = Path(state_root) / "launches.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(row.get("ts", ""))
        if ts is not None:
            out.append({**row, "ts": ts})
    return out


def _naive(ts):
    return ts.replace(tzinfo=None)


def _load_state(state_root):
    path = Path(state_root) / "detector.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state_root, data):
    path = Path(state_root) / "detector.json"
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def _status(path):
    m = re.match(r"---\n(.*?)\n---", path.read_text(errors="replace"), re.S)
    s = m and re.search(r"^status:\s*(\S+)", m.group(1), re.M)
    return s.group(1) if s else None


def detect(root, state_root, now):
    """Tasks that are `ready` and not advancing: list of (name, reason, runs,
    detail).

    Pure read. The per-night storm looks at the night `now` belongs to only:
    a storm from a past night is history, not something to block on today.
    Duties, fillers and `parallel: true` tasks (shards launched side by
    side) stay `ready` by contract and are paced by their own
    rules, so they are never judged here. Only runs after the detector last blocked a task count, so a
    task the owner sets back to `ready` gets a fresh start."""
    since = {k: _naive(datetime.fromisoformat(v))
             for k, v in _load_state(state_root).get("blocked", {}).items()}
    epoch = datetime.min
    launches = defaultdict(list)
    for row in _launches(state_root):
        if _naive(row["ts"]) > since.get(row["task"], epoch):
            launches[row["task"]].append(row)
    runs = defaultdict(list)
    for row in orchestrate_runs(state_root):
        if row["ts"] > since.get(row["task"], epoch):
            runs[row["task"]].append(row)
    found = []
    for path in sorted(Path(root, "tasks").glob("*.md")):
        name = path.name
        fm = path.read_text(errors="replace")
        if (_status(path) != "ready"
                or re.search(r"^(duty:\s*\S|filler:\s*true|parallel:\s*true)", fm, re.M)):
            continue
        ls = launches.get(name, [])
        if (len(ls) >= STALL_RUNS and ls[-1]["hash"] == ls[-2]["hash"]
                and content_hash(path) == ls[-1]["hash"]):
            found.append((name, "stalled", len(ls),
                          f"{len(ls)} launches, file unchanged since the last one"))
            continue
        rs = runs.get(name, [])
        night = (_naive(now) - NIGHT_SHIFT).date()
        tonight = len(_own_launches(
            [r for r in rs if (r["ts"] - NIGHT_SHIFT).date() == night]))
        if tonight >= STORM_NIGHT_RUNS:
            found.append((name, "storming", len(rs),
                          f"{tonight} launches in the night of {night}"))
            continue
        tail = [r["exit"] for r in rs[-STORM_SAME_EXIT:]]
        if (len(tail) == STORM_SAME_EXIT and tail[0] not in (0, TIMEOUT_EXIT)
                and len(set(tail)) == 1):
            found.append((name, "storming", len(rs),
                          f"last {STORM_SAME_EXIT} sessions all exited {tail[0]}"))
    return found


def _own_launches(runs):
    """One night's runs of one task minus the relaunches the scheduler caused.

    A run whose slice is shorter than the previous run's was clamped by the
    window end: the task was relaunched because the scheduler cut its last
    slice short, not because it keeps failing. On 2026-09-27 such a ladder
    (33/23/18/13/7 minutes, each session doing real work) was charged to the
    task as a storm and blocked it.

    Only a relaunch after a run that did not fail counts as the scheduler's:
    after a failure, a shorter slice does not explain the relaunch away."""
    own, prev = [], None
    for r in sorted(runs, key=lambda r: r["ts"]):
        clamped = (prev is not None and r["slice"] is not None
                   and prev["slice"] is not None and r["slice"] < prev["slice"]
                   and prev["exit"] in (0, TIMEOUT_EXIT))
        if not clamped:
            own.append(r)
        prev = r
    return own


def _local(ts, tz):
    """A timestamp as naive wall-clock time in `tz` (the scheduler's zone)."""
    return ts.astimezone(tz).replace(tzinfo=None) if ts.tzinfo else ts


def _night(ts, tz):
    """The night bucket of a timestamp, in the scheduler's local time."""
    return (_local(ts, tz) - NIGHT_SHIFT).date()


def ladder_blocked(state_root, now, slice_min):
    """Queue tasks that must not be relaunched into a slice of `slice_min`.

    A task whose last session tonight had a longer slice and used less than
    half of it gains nothing from a shorter one: it would only pay another
    cold start for less time. Relaunching into shorter and shorter slices is
    how 2026-09-27 cut one task into five sessions. A task whose last session
    used at least half its slice was working, not guessing, and stays
    eligible: its work is not done (it is `ready` again) and a shorter slice
    still advances it."""
    night = _night(now, now.tzinfo)
    last = {}
    for row in ledger_rows(state_root):
        if row.get("mode") != "orchestrate" or row.get("task") in (None, "auto"):
            continue
        ts = _parse_ts(row["ts"])
        if (ts is None or _night(ts, now.tzinfo) != night
                or _local(ts, now.tzinfo) > _local(now, now.tzinfo)):
            continue
        last[task_name(row["task"])] = row
    out = set()
    for name, row in last.items():
        if row.get("duration_ms") is None:
            # No result JSON: killed at the slice's hard limit or crashed.
            # Either way it did not exit early by its own judgement.
            continue
        prev = int(row.get("slice_min") or 0)
        used_min = float(row["duration_ms"]) / 60000
        if slice_min < prev and used_min < prev / 2:
            out.add(name)
    return out


def early_exit(slice_min, duration_min, status):
    """Whether a session left more than half its slice unused while its task
    still has work left (`ready`, or `in-progress` if it never released it).

    A task finished (`done`) or handed to the owner (`blocked`) early is a
    good outcome. One sent back to `ready` after a few minutes of a long slice
    is the 2026-09-27 pattern: the session guessed its time was up."""
    return (status in ("ready", "in-progress")
            and float(duration_min) < float(slice_min) / 2)


BLOCK_NOTE = (
    "- {date}: blocked by the gatekeeper's stall detector: {reason} "
    "({detail}, {runs} runs counted). The task is not advancing between "
    "sessions. Decide whether to rewrite its spec (sharper definition of done, "
    "smaller scope), fix what keeps failing, or drop it; then set it back to "
    "`ready`.\n")


def block_detected(root, state_root, now, set_status):
    """Write every detected task `blocked` with a note; returns what was blocked.

    `set_status` is lib/tasks.set_status, injected to keep this module free of
    the scheduler's imports."""
    found = detect(root, state_root, now)
    if not found:
        return []
    data = _load_state(state_root)
    blocked = data.setdefault("blocked", {})
    history = data.setdefault("history", [])
    for name, reason, runs, detail in found:
        set_status(Path(root, "tasks", name), "blocked",
                   BLOCK_NOTE.format(date=now.strftime("%F"), reason=reason,
                                     detail=detail, runs=runs))
        blocked[name] = _naive(now).isoformat()
        history.append({"ts": _naive(now).isoformat(), "task": name,
                        "reason": reason, "runs": runs, "detail": detail})
    _save_state(state_root, data)
    return found


def history(state_root):
    """Every block the detector has written, oldest first."""
    return _load_state(state_root).get("history", [])


def active_blocks(root, state_root):
    """History entries whose task is still `blocked`, latest block per task.

    A task the owner set back to `ready` (or finished) is no longer a stall to
    report, even though its block stays in the history."""
    latest = {h["task"]: h for h in history(state_root)}
    return [h for name, h in sorted(latest.items())
            if (Path(root, "tasks", name).exists()
                and _status(Path(root, "tasks", name)) == "blocked")]


def _zero_work_failure(row):
    return (row.get("exit", 0) not in (0, TIMEOUT_EXIT) and not float(row.get("cost_usd") or 0)
            and not int(row.get("duration_ms") or 0))


def global_failure(state_root):
    """The last GLOBAL_FAIL_RUNS orchestrate sessions, any task, if they all
    exited non-zero at zero cost and zero duration and were not already
    reported; None otherwise.

    Per-task detection cannot see this: when every launch fails (the bash 3.2
    bug of 2026-09-16/17, 70 of 88 sessions exiting 1 at $0), every task
    fails once and the queue just moves on."""
    rows = [r for r in ledger_rows(state_root) if r.get("mode") == "orchestrate"]
    tail = rows[-GLOBAL_FAIL_RUNS:]
    if len(tail) < GLOBAL_FAIL_RUNS or not all(map(_zero_work_failure, tail)):
        return None
    if _load_state(state_root).get("global_tripped_at", "") >= tail[-1]["ts"]:
        return None
    return tail


def _last_stderr_line(path):
    try:
        lines = [l for l in Path(path).read_text(errors="replace").splitlines()
                 if l.strip()]
    except OSError:
        return "(no launchd.gatekeeper.log)"
    return lines[-1].strip() if lines else "(launchd.gatekeeper.log is empty)"


def trip_global(state_root, paused, needs):
    """Pause the gatekeeper on a launch-failure storm; returns the failed rows
    when it tripped, None otherwise. Reported once per storm: a row already
    reported never trips again, so the owner removing PAUSED is final."""
    tail = global_failure(state_root)
    if not tail:
        return None
    exits = sorted({str(r.get("exit")) for r in tail})
    stderr = _last_stderr_line(Path(state_root) / "launchd.gatekeeper.log")
    Path(paused).write_text(
        f"paused by the stall detector: {len(tail)} consecutive zero-work "
        f"failures, {tail[0]['ts']} to {tail[-1]['ts']}\n")
    with open(needs, "a") as f:
        f.write(f"- [ ] orchestrator/PAUSED: the gatekeeper paused itself after "
                f"{len(tail)} consecutive orchestrate sessions exited non-zero "
                f"(exit {','.join(exits)}) at zero cost and zero duration, from "
                f"{tail[0]['ts']} to {tail[-1]['ts']}. Last line of "
                f"launchd.gatekeeper.log: `{stderr}`. Fix the launch failure, "
                f"then delete orchestrator/PAUSED.\n")
    data = _load_state(state_root)
    data["global_tripped_at"] = tail[-1]["ts"]
    _save_state(state_root, data)
    return tail


if __name__ == "__main__":
    import sys
    # early-exit <slice_min> <duration_min> <task_file>: exit 0 when early.
    if len(sys.argv) == 5 and sys.argv[1] == "early-exit":
        path = Path(sys.argv[4])
        status = _status(path) if path.exists() else None
        sys.exit(0 if early_exit(sys.argv[2], sys.argv[3], status) else 1)
    print("usage: stalls.py early-exit <slice_min> <duration_min> <task_file>",
          file=sys.stderr)
    sys.exit(2)
