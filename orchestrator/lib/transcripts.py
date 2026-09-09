"""Token usage read straight from Claude Code's own transcript files.

This replaces `npx ccusage blocks --json`, which was the source until
2026-09-09. Three reasons, in order of how much they cost:

1. ccusage reports no per-model breakdown per 5h block. Fable has its own,
   separate limit (see lib/quota.py), so consumption that cannot tell the
   models apart cannot show which one is running out.
2. ccusage anchors a block at the whole hour preceding its first entry, while
   the real quota window starts at the first token. Up to 59 minutes of a
   night were given away to that rounding.
3. It was a `npx -y ccusage@latest` subprocess on every tick: a package
   resolution (network) inside the decider, with a 120 s timeout, for data
   that is sitting in local files.

The format read here is the one Claude Code writes: one JSON object per line
under `<config_dir>/projects/<slug>/<session>.jsonl`, where an assistant entry
carries `timestamp`, `message.model` and `message.usage`. Entries are
de-duplicated on (message id, request id) because a resumed or forked session
replays earlier entries into the new file.
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

WINDOW = timedelta(hours=5)

# CLI model ids -> the engine's model families (MODEL_RANK in lib/tasks.py).
# Matched by prefix so a point release (claude-fable-5-1) needs no entry.
FAMILY_PREFIXES = (
    ("claude-fable", "fable"),
    ("claude-opus", "opus"),
    ("claude-sonnet", "sonnet"),
    ("claude-haiku", "haiku"),
)


# Claude Code writes assistant entries of its own making (API error notices,
# interrupt markers) with this model id and a zero-token usage block. They are
# not consumption and must not be reported as an unrecognized model.
SYNTHETIC = "<synthetic>"


def family(model_id):
    """The engine's family for a CLI model id, or None when unrecognized.

    Unrecognized ids are counted under "unknown" by the caller and reported,
    rather than dropped: an id we cannot classify is still consuming the
    owner's quota, and the honest failure is a visible warning, not silently
    free tokens.
    """
    m = str(model_id or "").lower()
    for prefix, fam in FAMILY_PREFIXES:
        if m.startswith(prefix):
            return fam
    return None


def _entry_tokens(usage):
    return (int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("output_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0))


def _files(config_dir):
    root = Path(config_dir) / "projects"
    if not root.is_dir():
        return []
    return sorted(root.glob("*/*.jsonl"))


def entries(config_dir, since=None):
    """Yield (timestamp, model_id, tokens), oldest first, de-duplicated.

    `since` drops older entries early; it is an optimisation only, the result
    is the same as filtering afterwards. Unreadable lines are skipped: a
    transcript being appended to while we read it can end in a partial line,
    and that must not take the decider down.
    """
    seen = set()
    out = []
    for path in _files(config_dir):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    msg = e.get("message") or {}
                    usage = msg.get("usage")
                    if not usage or not e.get("timestamp"):
                        continue
                    if msg.get("model") == SYNTHETIC:
                        continue
                    key = (msg.get("id"), e.get("requestId"))
                    if key != (None, None):
                        if key in seen:
                            continue
                        seen.add(key)
                    try:
                        ts = datetime.fromisoformat(
                            str(e["timestamp"]).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if since is not None and ts < since:
                        continue
                    out.append((ts, msg.get("model"), _entry_tokens(usage)))
        except OSError:
            continue
    out.sort(key=lambda r: r[0])
    return out


def blocks(rows):
    """Group entries into 5h quota windows, as the account's limit sees them.

    A window opens on the first entry and lasts exactly `WINDOW` from that
    instant; the first entry at or after it opens the next one. This is the
    account behaviour the owner sees in `/usage` ("Resets 4:20am" for a first
    token at 23:20), with no rounding to the hour.
    """
    out = []
    for ts, model, tokens in rows:
        if not out or ts >= out[-1]["start"] + WINDOW:
            out.append({"start": ts, "end": ts + WINDOW, "by_model": {},
                        "tokens": 0, "last": ts})
        b = out[-1]
        b["tokens"] += tokens
        b["last"] = ts
        b["by_model"][model] = b["by_model"].get(model, 0) + tokens
    return out


def by_family(by_model, unknown=None):
    """{family: tokens} from a {model_id: tokens} map.

    Ids of no known family are counted under "unknown" and appended to
    `unknown` when a list is given, so a model id the engine has never seen
    shows up in `gate.py status` instead of quietly costing nothing.

    These totals are a MEASURE, not a prediction of the account's limit: the
    limit is not linear in local tokens (lib/quota.py explains why, with the
    measurement). They pace the week and they say which model is doing the
    spending; they do not say when a model is about to be cut off.
    """
    out = {}
    for model_id, tokens in by_model.items():
        fam = family(model_id)
        if fam is None:
            if unknown is not None and model_id not in unknown:
                unknown.append(model_id)
            fam = "unknown"
        out[fam] = out.get(fam, 0) + tokens
    return out


def summary(config_dir, since, now):
    """Everything the controller needs about consumption, in one pass."""
    unknown = []
    rows = entries(config_dir, since=since)
    week_by_model = {}
    for _ts, model, tokens in rows:
        week_by_model[model] = week_by_model.get(model, 0) + tokens
    week = by_family(week_by_model, unknown)
    active = None
    for b in blocks(rows):
        if b["start"] <= now < b["end"]:
            active = {"start": b["start"], "end": b["end"], "active": True,
                      "tokens": b["tokens"],
                      "by_family": by_family(b["by_model"], unknown)}
    return {
        "week_tokens": sum(week.values()),
        "week_by_family": week,
        "block": active,
        "unknown_models": unknown,
    }


def default_config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
