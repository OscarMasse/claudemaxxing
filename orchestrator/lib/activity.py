"""How long since the owner last used Claude interactively?

Scans session transcripts under the Claude profile's projects dir, excluding
the orchestrator's own project directory (its headless sessions run from the
backlog root and must not count as the owner's activity).

Idle is derived from the timestamp of the last INTERACTIVE event (user or
assistant turn) inside each transcript, never from file mtime and never from
system events. Two live failure modes motivated this (observed 2026-08-19):
- external processes (transcript compressors, indexers) rewrite session files
  without appending events, bumping mtime;
- Claude Code itself appends timestamped housekeeping events (away_summary,
  turn_duration, stop_hook_summary) to idle sessions.
Either one would reset the idle clock hourly and starve the night regime.
"""
import json
from datetime import datetime
from pathlib import Path

_TAIL_BYTES = 65536
_INTERACTIVE_TYPES = ("user", "assistant")


def _last_event_ts(path):
    """Epoch of the newest interactive event in the file's tail, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            ts = event.get("timestamp")
        except (json.JSONDecodeError, AttributeError):
            continue
        if event.get("type") not in _INTERACTIVE_TYPES or not isinstance(ts, str):
            continue
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


def idle_minutes(projects_dir, exclude_dirname, now_ts):
    newest = None
    root = Path(projects_dir)
    if not root.is_dir():
        return None
    for proj in root.iterdir():
        if not proj.is_dir() or proj.name == exclude_dirname:
            continue
        for f in proj.glob("*.jsonl"):
            # mtime is always >= the last event written, so a file older than
            # the current best cannot win; skip the read.
            if newest is not None and f.stat().st_mtime <= newest:
                continue
            ts = _last_event_ts(f)
            if ts is not None and (newest is None or ts > newest):
                newest = ts
    if newest is None:
        return None
    return max(0.0, (now_ts - newest) / 60.0)
