"""Observed quota exhaustion, per model family.

Why this exists instead of a bigger token budget model
------------------------------------------------------
On the 2026-09-09 night, three consecutive Fable sessions died on
`You've hit your session limit - resets 4:20am (Europe/Warsaw)` while a Sonnet
session in the same window ran to completion, and while the controller still
believed it had tens of millions of tokens of headroom. The obvious fix looked
like "count tokens per model and give Fable its own cap". It cannot work, and
the measurement says so:

- The account's limit is not linear in the tokens we can count. Over 95% of
  local token volume is cache reads, which the limit clearly discounts by some
  unpublished factor.
- Worse, no non-negative weighting of (input, output, cache read, cache write)
  can even reproduce the observed bars. Measured on the two `/usage`
  screenshots of that night: the Fable weekly bar moved from 37% to 51%
  (a delta/cumulative ratio of 0.27) while the largest ratio any pure
  component can produce from the local numbers is 0.18. The bars are
  server-side truth and include usage we cannot see (other devices, claude.ai).

So local accounting cannot predict the wall. What it CAN do is notice the wall
the moment we touch it: the account tells us, in one unambiguous line, which
limit is gone and when it comes back. That signal is exact, needs no
calibration, survives Anthropic changing the accounting, and is per model -
which is what the night showed, Fable dead and Sonnet fine.

The token budget in `controller.py` keeps its job, which was never this one:
pacing the week so background work does not eat the owner's daytime reserve.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

STATE_FILE = "exhausted.json"

# Both forms the account uses:
#   "You've hit your session limit \u00b7 resets 4:20am (Europe/Warsaw)"
#   "You've hit your weekly limit \u00b7 resets Sep 11 at 6am (Europe/Warsaw)"
# The month/day part is optional; without it the reset is the next occurrence
# of that clock time.
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec")
_LIMIT_RE = re.compile(
    r"hit your (?P<scope>\w+) limit.*?resets\s+"
    r"(?:(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s+at\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"(?:\s*\((?P<tz>[\w/+-]+)\))?",
    re.I | re.S)

FALLBACK = timedelta(hours=5)  # one quota window, when no reset time is given


def parse(text, now, default_tz="UTC"):
    """(scope, reset_datetime) from a limit message, or None.

    `now` anchors the reset to the next occurrence of that clock time, since
    the message states a time of day and not a date.
    """
    m = _LIMIT_RE.search(text or "")
    if not m:
        return None
    scope = m.group("scope").lower()
    try:
        tz = ZoneInfo(m.group("tz") or default_tz)
    except Exception:
        tz = ZoneInfo(default_tz)
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    local = now.astimezone(tz)
    reset = local.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    month_name = (m.group("month") or "")[:3].lower()
    if month_name in MONTHS:
        month = MONTHS.index(month_name) + 1
        day = int(m.group("day"))
        # A stated month/day is in the future by construction; roll the year
        # only when that puts it in the past (a reset in early January stated
        # in late December).
        reset = reset.replace(month=month, day=day)
        if reset <= local:
            reset = reset.replace(year=reset.year + 1)
    elif reset <= local:
        reset += timedelta(days=1)
    return scope, reset


def _read(state):
    f = state / STATE_FILE
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except (ValueError, OSError):
        return {}


def _write(state, data):
    state.mkdir(parents=True, exist_ok=True)
    tmp = state / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(state / STATE_FILE)


def record(state, family, text, now, default_tz="UTC"):
    """Note that `family` hit a limit, if `text` says so. Returns the record.

    Called on every failed session, with that session's own stderr. A session
    that failed for any other reason leaves no record, so an ordinary crash
    never costs the model its eligibility.
    """
    hit = parse(text, now, default_tz)
    if not hit:
        return None
    scope, reset = hit
    entry = {"scope": scope, "until": reset.isoformat(), "seen": now.isoformat()}
    data = _read(state)
    data[family] = entry
    _write(state, data)
    return entry


def record_fallback(state, family, now, until=None):
    """Note an exhaustion with no parseable reset time: block for one window."""
    entry = {"scope": "unknown", "until": (until or now + FALLBACK).isoformat(),
             "seen": now.isoformat()}
    data = _read(state)
    data[family] = entry
    _write(state, data)
    return entry


def blocked(state, now):
    """{family: reset_datetime} for the families still out of quota.

    Entries whose reset has passed are simply ignored rather than deleted, so
    `status` can still show that a family was exhausted earlier tonight.
    """
    out = {}
    for family, entry in _read(state).items():
        try:
            until = datetime.fromisoformat(entry["until"])
        except (KeyError, TypeError, ValueError):
            continue
        if until > now:
            out[family] = until
    return out


def history(state):
    """The raw records, for reporting."""
    return _read(state)


def _main(argv):
    """CLI for run.sh: `quota.py record <state_dir> <model> <stderr_file>`.

    Prints a line only when a limit was actually detected, so an ordinary
    failure stays quiet in runs.out.
    """
    if len(argv) != 4 or argv[0] != "record":
        print("usage: quota.py record <state_dir> <model> <stderr_file>")
        return 2
    _cmd, state_dir, model, err_file = argv
    try:
        with open(err_file, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return 0
    entry = record(Path(state_dir), model, text, datetime.now().astimezone())
    if entry:
        print(f"quota exhausted model={model} scope={entry['scope']} "
              f"until={entry['until']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
