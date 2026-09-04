"""Estimate quota consumption from ccusage (local JSONL analysis, no network auth).

Per account: the merged account config (see config.accounts) carries the
account's `name` and `claude_config_dir`, and ccusage reads that profile's
transcripts, so each subscription's consumption is measured independently.

week_tokens = sum of 5h-block totals since the account's last weekly reset.
block       = the currently active 5h block, if any.
Tests / manual runs: set ORCH_CCUSAGE_JSON_<ACCOUNT> (name upper-cased,
non-alphanumerics -> _) or ORCH_CCUSAGE_JSON to read a fixture instead of npx.
"""
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from . import controller


def env_name(account_name):
    """ACCOUNT-NAME -> ACCOUNT_NAME, for per-account env overrides."""
    return re.sub(r"[^A-Z0-9]", "_", str(account_name).upper())


def _fetch(cfg):
    fixture = (os.environ.get(f"ORCH_CCUSAGE_JSON_{env_name(cfg.get('name', 'default'))}")
               or os.environ.get("ORCH_CCUSAGE_JSON"))
    if fixture:
        with open(fixture) as f:
            return json.load(f)
    # ccusage locates transcripts via CLAUDE_CONFIG_DIR; pin it to the profile
    # of the account being scheduled (launchd can leave the var unset anyway).
    cfg_dir = cfg.get("claude_config_dir") or str(Path.home() / ".claude")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg_dir)
    out = subprocess.run(
        ["npx", "-y", "ccusage@latest", "blocks", "--json"],
        capture_output=True, text=True, timeout=120, check=True, env=env)
    return json.loads(out.stdout)


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def snapshot(cfg, now):
    data = _fetch(cfg)
    since = controller.prev_reset(cfg, now)
    week, active = 0, None
    for b in data.get("blocks", []):
        if b.get("isGap"):
            continue
        start = _ts(b["startTime"])
        if start >= since:
            week += int(b.get("totalTokens", 0))
        if b.get("isActive"):
            active = {"start": start, "end": _ts(b["endTime"]),
                      "tokens": int(b.get("totalTokens", 0)), "active": True}
    return {"week_tokens": week, "block": active, "account": cfg.get("name", "default")}
