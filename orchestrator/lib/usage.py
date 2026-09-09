"""Measure quota consumption for one account, from local transcripts.

Per account: the merged account config (see config.accounts) carries the
account's `name` and `claude_config_dir`, and only that profile's transcripts
are read, so each subscription's consumption is measured independently.

week_tokens = tokens since the account's last weekly reset.
block       = the currently active 5h quota window, if any, with its real
              start (the first token of the window, not the top of that hour).

These numbers pace the week. They do NOT predict when a model runs out of
quota - the account's limit is not linear in the tokens we can count here.
That job belongs to lib/quota.py, which reads the account's own limit message.

Tests / manual runs: ORCH_USAGE_JSON_<ACCOUNT> (name upper-cased,
non-alphanumerics -> _) or ORCH_USAGE_JSON points at a fixture and replaces
the transcript scan. A fixture is either a snapshot ({"week_tokens": ...,
"block": {...}}) or the ccusage `blocks` shape this module used to read.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import controller, transcripts


def env_name(account_name):
    """ACCOUNT-NAME -> ACCOUNT_NAME, for per-account env overrides."""
    return re.sub(r"[^A-Z0-9]", "_", str(account_name).upper())


def _fixture_path(cfg):
    name = env_name(cfg.get("name", "default"))
    # ORCH_CCUSAGE_JSON* is the pre-2026-09-09 name, still honoured so an
    # existing fixture or a saved reproduction keeps working.
    for var in (f"ORCH_USAGE_JSON_{name}", "ORCH_USAGE_JSON",
                f"ORCH_CCUSAGE_JSON_{name}", "ORCH_CCUSAGE_JSON"):
        path = os.environ.get(var)
        if path:
            return path
    return None


def _ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _from_fixture(path, since, now):
    with open(path) as f:
        data = json.load(f)
    if "blocks" in data:
        week, active = 0, None
        for b in data["blocks"]:
            if b.get("isGap"):
                continue
            start = _ts(b["startTime"])
            if start >= since:
                week += int(b.get("totalTokens", 0))
            if b.get("isActive"):
                active = {"start": start, "end": _ts(b["endTime"]),
                          "tokens": int(b.get("totalTokens", 0)), "active": True,
                          "by_family": {}}
        return {"week_tokens": week, "week_by_family": {}, "block": active,
                "unknown_models": []}
    block = data.get("block")
    if block:
        block = dict(block, start=_ts(block["start"]), end=_ts(block["end"]),
                     active=True, by_family=block.get("by_family", {}))
    return {"week_tokens": int(data.get("week_tokens", 0)),
            "week_by_family": data.get("week_by_family", {}),
            "block": block,
            "unknown_models": data.get("unknown_models", [])}


def snapshot(cfg, now):
    since = controller.prev_reset(cfg, now)
    fixture = _fixture_path(cfg)
    if fixture:
        snap = _from_fixture(fixture, since, now)
    else:
        cfg_dir = cfg.get("claude_config_dir") or str(Path.home() / ".claude")
        snap = transcripts.summary(cfg_dir, since, now)
    snap["account"] = cfg.get("name", "default")
    return snap
