"""Measure quota consumption for one account, from local transcripts.

Per account: the merged account config (see config.accounts) carries the
account's `name` and `claude_config_dir`, and only that profile's transcripts
are read, so each subscription's consumption is measured independently.

week_usd = USD at list price since the account's last weekly reset
           (lib/transcripts.py has the price table and why the unit is money).
block    = the currently active 5h quota window, if any, with its real start
           (the first token of the window, not the top of that hour).

These numbers pace the week. The calibration against `/usage` is coarse, so
when a model runs out of quota is still observed by lib/quota.py, which reads
the account's own limit message, rather than predicted from here.

Tests / manual runs: ORCH_USAGE_JSON_<ACCOUNT> (name upper-cased,
non-alphanumerics -> _) or ORCH_USAGE_JSON points at a fixture and replaces
the transcript scan. A fixture is a snapshot as lib/transcripts.summary()
returns it: {"week_usd": ..., "week_by_family": {...}, "block": {..., "usd":
...} | null, "unknown_models": [...]}. The ccusage `blocks` shape and the
ORCH_CCUSAGE_JSON* names are gone with the token unit (2026-09-12).
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
    for var in (f"ORCH_USAGE_JSON_{name}", "ORCH_USAGE_JSON"):
        path = os.environ.get(var)
        if path:
            return path
    return None


def _ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _from_fixture(path):
    with open(path) as f:
        data = json.load(f)
    block = data.get("block")
    if block:
        block = dict(block, start=_ts(block["start"]), end=_ts(block["end"]),
                     active=True, by_family=block.get("by_family", {}))
    return {"week_usd": float(data.get("week_usd", 0)),
            "week_by_family": data.get("week_by_family", {}),
            "block": block,
            "unknown_models": data.get("unknown_models", [])}


def snapshot(cfg, now):
    since = controller.prev_reset(cfg, now)
    fixture = _fixture_path(cfg)
    if fixture:
        snap = _from_fixture(fixture)
    else:
        cfg_dir = cfg.get("claude_config_dir") or str(Path.home() / ".claude")
        snap = transcripts.summary(cfg_dir, since, now)
    snap["account"] = cfg.get("name", "default")
    return snap
