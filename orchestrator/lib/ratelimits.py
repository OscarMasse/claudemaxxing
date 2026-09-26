"""Quota caps derived from the account's own rate-limit readings.

Claude Code hands the status line command `rate_limits.five_hour` and
`rate_limits.seven_day` (`used_percentage`, `resets_at` in epoch seconds) for
Pro and Max accounts (https://code.claude.com/docs/en/statusline). That is the
data behind `/usage`, from a documented source, with no token handling.

The status line script pipes its stdin to `ratelimits.py record <account>`,
which appends a reading to `state/<account>/rate_limits.jsonl` together with
what the engine itself measured over the same period (lib/transcripts.py).
Each reading then yields a cap: `cap = engine_usd / (used_percentage / 100)`.

Why the caps are re-derived continuously instead of entered by hand: the
limits move (the September 2026 promo went +50% then +25%) and the hand
values drifted about 2x before anyone noticed (2026-09-24: the engine
believed the week 62% spent while `/usage` read 31%). Every derived cap is
provisional; each new period refreshes it.

Noise rules (the owner's decisions of 2026-09-25):

- Only readings at >= 10% of the week and >= 20% of the window count, which
  bounds the error of a whole-percent reading to 5%. The day's usable
  readings are reduced to their median.
- A reading pairs its percentage with the engine's USD over the reading's own
  period (bounded by its `resets_at`), never with another period's.
- Day medians within 15% of the cap in use are the same limit seen again, and
  the cap is the median of those days over the last week. A day median
  further than 15% away is a limit change: it replaces the cap outright, no
  averaging, and is reported.
- `p90_daily_usd` scales with the weekly cap, at the ratio the seed fixed.

At night no reading arrives (the status line only renders in interactive
sessions): the engine keeps the latest derived caps and its own measurement
since. `lib/quota.py` stays the guard against the real wall. The per-model
(Fable) weekly figure is not in the status line data, and usage from other
devices or claude.ai is invisible to the engine and skews the ratio.

Bootstrap: the history is seeded once (`ratelimits.py seed`) with hand
readings; the first usable real reading supersedes the seed.
"""
import fcntl
import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import config, transcripts  # noqa: E402

HISTORY = "rate_limits.jsonl"
LOCK = "rate_limits.lock"
ERROR = "rate_limits.err"

# (status line key, period length, lowest usable percentage, derived cap key)
WINDOWS = (
    ("seven_day", timedelta(days=7), 10.0, "weekly_cap_usd"),
    ("five_hour", timedelta(hours=5), 20.0, "window_cap_usd"),
)
MIN_INTERVAL = timedelta(minutes=1)
CHANGE_RATIO = 0.15
# A cap is the median of the day medians of its regime over this many days
# at most, so a limit change smaller than CHANGE_RATIO still takes over
# within a week instead of being outvoted by every older day.
REGIME_SPAN = timedelta(days=7)
# How far back a limit change stays in `status` (and therefore the digest).
CHANGE_REPORT_AGE = timedelta(days=7)


def _ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _valid(row):
    """Whether a history row has the shape the fold reads (a bad row must not
    take the tick down, nor block every later recording)."""
    try:
        _ts(row["ts"])
        if row.get("seed"):
            return all(float(row[k]) > 0 for _key, _l, _p, k in WINDOWS) \
                and float(row["p90_daily_usd"]) >= 0
        for key, *_ in WINDOWS:
            if key in row:
                w = row[key]
                float(w["used_percentage"]), float(w["engine_usd"])
                _ts(w["resets_at"])
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def load(state_dir):
    """History rows, oldest first. Unreadable or malformed lines are skipped."""
    path = Path(state_dir) / HISTORY
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and _valid(row):
            rows.append(row)
    return rows


def _append(state_dir, row):
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(state_dir) / HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def seed(state_dir, weekly, window, p90, at):
    """Write the seed row once. Returns whether it wrote.

    Refused only when a seed already exists: readings recorded before the
    seed are kept, and the first usable one still supersedes it.
    """
    if any(r.get("seed") for r in load(state_dir)):
        return False
    _append(state_dir, {"ts": _ts(at).isoformat(), "seed": True,
                        "weekly_cap_usd": float(weekly),
                        "window_cap_usd": float(window),
                        "p90_daily_usd": float(p90)})
    return True


def parse(payload):
    """{key: (used_percentage, resets_at datetime)} from a status line payload."""
    out = {}
    limits = (payload or {}).get("rate_limits") or {}
    for key, _length, _min_pct, _cap in WINDOWS:
        w = limits.get(key) or {}
        pct, resets = w.get("used_percentage"), w.get("resets_at")
        if isinstance(pct, (int, float)) and isinstance(resets, (int, float)):
            out[key] = (float(pct), datetime.fromtimestamp(resets, timezone.utc))
    return out


def _same(row, readings):
    seen = {k: (w["used_percentage"], w["resets_at"])
            for k, *_ in WINDOWS if (w := row.get(k))}
    return seen == {k: (pct, resets.isoformat()) for k, (pct, resets) in readings.items()}


def record(acct, state_dir, payload, now, entries=None):
    """Append one reading unless the last one is under a minute old or equal.

    `entries` (tests) replaces the transcript scan. The throttle and the
    equality check run before the scan, which costs about a second; the lock
    keeps concurrent status lines of several sessions from double-writing.
    Returns the row written, or None.
    """
    readings = parse(payload)
    if not readings:
        return None
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(state_dir) / LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        last = next((r for r in reversed(load(state_dir)) if not r.get("seed")), None)
        if last and (now - _ts(last["ts"]) < MIN_INTERVAL or _same(last, readings)):
            return None
        lengths = {key: length for key, length, *_ in WINDOWS}
        starts = {k: resets - lengths[k] for k, (_pct, resets) in readings.items()}
        if entries is None:
            entries = transcripts.entries(acct["claude_config_dir"],
                                          since=min(starts.values()))
        row = {"ts": now.isoformat()}
        for key, (pct, resets) in readings.items():
            usd = sum(e[2] for e in entries if starts[key] <= e[0] <= now)
            row[key] = {"used_percentage": pct, "resets_at": resets.isoformat(),
                        "engine_usd": round(usd, 4)}
        _append(state_dir, row)
        return row


def _fold(rows, key, min_pct, seed_cap, seed_ts, now, tz):
    """One cap from the history: day medians folded into limit regimes."""
    days = {}
    for row in rows:
        w = row.get(key)
        if row.get("seed") or not w:
            continue
        pct, usd = float(w["used_percentage"]), float(w["engine_usd"])
        ts, resets = _ts(row["ts"]), _ts(w["resets_at"])
        if pct < min_pct or usd <= 0 or ts >= resets:
            continue
        day = days.setdefault(ts.astimezone(tz).date(), {"caps": [], "ts": ts, "resets": resets})
        day["caps"].append(usd / (pct / 100.0))
        day["ts"], day["resets"] = max(day["ts"], ts), max(day["resets"], resets)
    cap, source, as_of, resets, regime, changes = seed_cap, "seed", seed_ts, None, [], []
    for day in sorted(days):
        median = statistics.median(days[day]["caps"])
        if source == "seed" or abs(median - cap) > CHANGE_RATIO * cap:
            changes.append({"cap": key, "day": day.isoformat(), "from": cap, "to": median,
                            "why": "seed superseded" if source == "seed" else "limit change"})
            regime = []
        regime = [(d, m) for d, m in regime if day - d < REGIME_SPAN] + [(day, median)]
        cap = statistics.median(m for _d, m in regime)
        source, as_of, resets = "history", days[day]["ts"], days[day]["resets"]
    if resets is not None and now < resets:
        source = "reading"
    return {"cap": cap, "source": source, "as_of": as_of.astimezone(tz), "changes": changes}


def caps(rows, now, tz):
    """The caps in use, or None when the history has no seed.

    Returns {"weekly_cap_usd", "window_cap_usd", "p90_daily_usd": float,
    "detail": {cap key: {"cap", "source", "as_of", "changes"}}}; `source` is
    `reading` (the latest usable reading's period is still open), `history`
    (the latest one is from an earlier period) or `seed`.
    """
    s = next((r for r in rows if r.get("seed")), None)
    if s is None:
        return None
    tz = ZoneInfo(str(tz))
    now = now.astimezone(tz)  # a naive now is local time, as in the controller
    out = {"detail": {}, "tz": str(tz)}
    for key, _length, min_pct, cap_key in WINDOWS:
        d = _fold(rows, key, min_pct, float(s[cap_key]), _ts(s["ts"]), now, tz)
        for c in d["changes"]:
            c["cap"] = cap_key
        out[cap_key] = d["cap"]
        out["detail"][cap_key] = d
    out["p90_daily_usd"] = (float(s["p90_daily_usd"]) * out["weekly_cap_usd"]
                            / float(s["weekly_cap_usd"]))
    return out


def recent_changes(derived, now):
    """Cap replacements of the last week, for `status` and the digest."""
    today = now.astimezone(ZoneInfo(derived["tz"])).date()
    return [c for d in derived["detail"].values() for c in d["changes"]
            if (today - date.fromisoformat(c["day"])).days <= CHANGE_REPORT_AGE.days]


def _root():
    """The backlog root, resolved exactly as gate.py resolves it."""
    root = os.environ.get("ORCH_ROOT")
    return Path(root) if root else config.backlog_root()


def _account(name):
    cfg = config.load(config.resolve_path(_root()))
    for a in config.accounts(cfg):
        if a["name"] == name:
            return a
    raise LookupError(f"unknown account {name!r}")


def _main(argv):
    cmd, account = argv[1], argv[2]
    state_dir = _root() / "orchestrator" / "state" / account
    if cmd == "record":
        # Called from a status line: never fail loudly, never print. The last
        # error is kept next to the history, and `gate.py status` shows it.
        try:
            record(_account(account), state_dir, json.load(sys.stdin),
                   datetime.now(timezone.utc))
            (state_dir / ERROR).unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001 - the status line must survive
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / ERROR).write_text(
                f"{datetime.now(timezone.utc).isoformat()} {type(e).__name__}: {e}\n")
        return 0
    if cmd == "seed":
        weekly, window, p90, at = argv[3:7]
        wrote = seed(state_dir, weekly, window, p90, at)
        print("seeded" if wrote else "already seeded, skipped")
        return 0
    print(f"ratelimits: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
