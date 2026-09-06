#!/usr/bin/env python3
"""Gatekeeper decision CLI. Zero Claude tokens: pure local reads.

  gate.py tick    -> one decision line PER ACCOUNT, logs each decision:
                       RUN <account> <slice_min> <task> <model> <effort> <project>
                       SKIP <account> <reason>
                     (plus a global "SKIP paused" when the kill switch is set)
  gate.py status  -> human-readable per-account quota summary for the digest

Every account is scheduled independently: its own quota budget, its own
activity lock (idle = interactive use of THAT account's Claude profile), its
own parallel-slot ceilings, and its own state under state/<account>/. Two
accounts can both launch sessions in the same tick.

Env overrides (tests / manual runs):
  ORCH_ROOT (backlog root override, wins over BACKLOG_ROOT), BACKLOG_ROOT,
  ORCH_CONFIG (explicit config file, wins over $BACKLOG_ROOT/config.yaml and
  the repo default; see lib/config.resolve_path),
  ORCH_NOW (ISO), ORCH_IDLE_MIN, ORCH_IDLE_MIN_<ACCOUNT>,
  ORCH_CCUSAGE_JSON, ORCH_CCUSAGE_JSON_<ACCOUNT>, ORCH_NO_NOTIFY,
  ORCH_PLATFORM (<ACCOUNT> = name upper-cased, non-alphanumerics -> _)
"""
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import activity, config, controller, ledger, tasks, usage  # noqa: E402

LOCK_TTL_S = 4.5 * 3600
PRESENT_MIN = 10  # idle threshold under which the owner counts as "at the PC"


def platform_name():
    """Name of the platform/<os>/ adapter directory for this host.
    ORCH_PLATFORM overrides detection (tests, unusual setups)."""
    override = os.environ.get("ORCH_PLATFORM")
    if override:
        return override
    return {"darwin": "macos"}.get(sys.platform, sys.platform)


def notifications_disabled():
    # ORCH_NO_OSASCRIPT is the pre-seam name of ORCH_NO_NOTIFY; still honored
    # for one release so existing installs keep their suppression working.
    return bool(os.environ.get("ORCH_NO_NOTIFY")
                or os.environ.get("ORCH_NO_OSASCRIPT"))


def paths():
    env_root = os.environ.get("ORCH_ROOT")
    root = Path(env_root) if env_root else config.backlog_root()
    orch = root / "orchestrator"
    return {
        "root": root, "orch": orch,
        "config": config.resolve_path(root),
        "state": orch / "state",
        "paused": orch / "PAUSED",
        "log": orch / "state" / "gatekeeper.log",
        "needs": root / "NEEDS-HUMAN.md",
        "notified": orch / "state" / "notified.txt",
    }


def now_from_env(acct):
    raw = os.environ.get("ORCH_NOW")
    if raw:
        return datetime.fromisoformat(raw)
    return datetime.now(ZoneInfo(acct["reset_tz"]))


def idle_for_account(root, acct):
    """Idle minutes on THIS account's profile: interactive use of one
    subscription never blocks background work on another."""
    raw = (os.environ.get(f"ORCH_IDLE_MIN_{usage.env_name(acct['name'])}")
           or os.environ.get("ORCH_IDLE_MIN"))
    if raw is not None:
        return float(raw)
    # Claude Code stores transcripts under <config dir>/projects/<path-encoded-cwd>.
    # The orchestrator's own project dir (derived from the backlog root) is
    # excluded: its headless sessions must not count as the owner's activity.
    exclude = str(root).replace("/", "-")
    return activity.idle_minutes(Path(acct["claude_config_dir"]) / "projects",
                                 exclude, time.time())


def log(p, msg):
    p["state"].mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%F %T")
    with open(p["log"], "a") as f:
        f.write(f"{stamp} {msg}\n")


def notify_duty(p, idle):
    """One user notification per new unchecked question, only when the owner
    is present. Delivery goes through the platform notify.sh hook; a missing
    hook is not an error, the question still lands in NEEDS-HUMAN.md."""
    if idle is None or idle > PRESENT_MIN or not p["needs"].exists():
        return
    hook = Path(__file__).resolve().parent / "platform" / platform_name() / "notify.sh"
    seen = set()
    if p["notified"].exists():
        seen = set(p["notified"].read_text().splitlines())
    new = []
    for line in p["needs"].read_text().splitlines():
        if not line.strip().startswith("- [ ]"):
            continue
        h = hashlib.sha1(line.strip().encode()).hexdigest()
        if h not in seen:
            new.append((h, line.strip()[6:].strip()))
    for h, question in new:
        if not notifications_disabled() and hook.exists():
            subprocess.run([str(hook), "Backlog needs you", question[:120]],
                           capture_output=True)
        p["state"].mkdir(parents=True, exist_ok=True)
        with open(p["notified"], "a") as f:
            f.write(h + "\n")


def active_slots(p, state_dir):
    """Count fresh RUNNING* locks in one account's state dir; break stale ones."""
    n = 0
    if not state_dir.is_dir():
        return 0
    for lock in sorted(state_dir.glob("RUNNING*")):
        try:
            started = float(lock.read_text().split()[1])
        except (IndexError, ValueError, OSError):
            started = 0.0
        if time.time() - started < LOCK_TTL_S:
            n += 1
        else:
            lock.unlink(missing_ok=True)
            log(p, f"broke stale lock {state_dir.name}/{lock.name}")
    return n


def tick_account(p, acct, projs):
    """One scheduling decision for one account. Returns the account's idle
    minutes (for the presence-gated notifications)."""
    name = acct["name"]
    now = now_from_env(acct)
    idle = idle_for_account(p["root"], acct)
    snap = usage.snapshot(acct, now)
    d = controller.decide(acct, now, snap, idle)
    state = p["state"] / name
    # Parallel slots: several sessions may run at once, up to the regime's
    # per-account cap.
    cap_slots = int(acct.get("night_parallel", 1)) if d.regime in ("night", "prereset") \
        else int(acct.get("day_parallel", 1))
    free = cap_slots - active_slots(p, state)
    if d.action == "run" and free <= 0:
        print(f"SKIP {name} running")
        return idle
    # Task selection happens here (not in the session): the tasks' declared
    # models must be known before launch. A task's model is a floor: the
    # ceiling per regime is sonnet (day), opus (night), or the margin-driven
    # tick model (prereset), and each session launches at max(floor, tick
    # model) - upgrades in the burn-down, never downgrades.
    # Slot count is budget-driven: estimated burn per session (measured rates
    # from the ledger, per-model defaults otherwise) must fit the slice budget.
    # A heavy task that alone consumes the budget gets exactly one session.
    picked = []
    if d.action == "run":
        max_floor = {"day": "sonnet", "night": "opus"}.get(d.regime, d.model)
        candidates = tasks.pick_multi(p["root"], projs, name,
                                      max_model=max_floor, count=free)
        measured = ledger.rates(state)
        defaults = {"sonnet": float(acct.get("est_rate_sonnet_per_min", 400000)),
                    "opus": float(acct.get("est_rate_opus_per_min", 800000)),
                    "fable": float(acct.get("est_rate_fable_per_min", 1200000))}
        budget = d.budget_tokens
        for cand in candidates:
            if tasks.MODEL_RANK[d.model] > tasks.MODEL_RANK[cand["model"]]:
                cand["model"] = d.model
            rate = measured.get((cand["path"], cand["model"])) \
                or defaults.get(cand["model"], defaults["sonnet"])
            est_burn = rate * d.slice_min
            if picked and est_burn > budget:
                break  # the first session always runs; extras must fit the budget
            picked.append(cand)
            budget -= est_burn
    log(p, f"account={name} {d.action} reason={d.reason!r} slice={d.slice_min} "
           f"regime={d.regime} model={d.model} week={snap['week_tokens']} "
           f"idle={idle} free_slots={free} "
           f"tasks={[t['path'] for t in picked] or '-'}")
    if d.action == "run" and not picked:
        print(f"SKIP {name} no eligible task")
        return idle
    if d.action == "run" and acct.get("dry_run"):
        log(p, f"account={name} dry_run: suppressed launch")
        print(f"SKIP {name} dry_run (would RUN {d.slice_min} x{len(picked)})")
        return idle
    if d.action == "run":
        for t in picked:
            print(f"RUN {name} {d.slice_min} {t['path']} {t['model']} "
                  f"{t['effort']} {t['project']}")
    else:
        print(f"SKIP {name} {d.reason}")
    return idle


def tick(p):
    if p["paused"].exists():
        print("SKIP paused")
        return
    cfg = config.load(p["config"])
    projs = config.projects(cfg)
    idles = []
    for acct in config.accounts(cfg):
        idles.append(tick_account(p, acct, projs))
    known = [i for i in idles if i is not None]
    notify_duty(p, min(known) if known else None)


def status(p):
    cfg = config.load(p["config"])
    projs = config.projects(cfg)
    for acct in config.accounts(cfg):
        name = acct["name"]
        now = now_from_env(acct)
        idle = idle_for_account(p["root"], acct)
        snap = usage.snapshot(acct, now)
        reset = controller.next_reset(acct, now)
        days = (reset - now).total_seconds() / 86400
        promo = (acct["promo_multiplier"]
                 if now.astimezone(ZoneInfo(acct["reset_tz"])).date().isoformat()
                 <= str(acct["promo_until"])
                 else 1.0)
        cap = acct["weekly_cap_tokens"] * promo
        reserve = acct["p90_daily_tokens"] * days
        available = cap - snap["week_tokens"] - reserve
        print(f"account={name}")
        print(f"week_tokens={snap['week_tokens']}")
        print(f"cap={cap:.0f} reserve={reserve:.0f} available={available:.0f}")
        print(f"next_reset={reset.isoformat()} days_remaining={days:.2f}")
        print(f"idle_min={idle} promo_until={acct['promo_until']}")
        for task_name, unmet in tasks.blocked(p["root"], projs, name):
            print(f"blocked task={task_name} unmet={' '.join(unmet)}")
        costs = ledger.stats(p["state"] / name)
        for task, s in sorted(costs.items()):
            print(f"cost task={task} runs={s['runs']} usd={s['cost_usd']:.2f} "
                  f"out_tokens={s['out_tokens']} total_tokens={s['total_tokens']}")


def main():
    p = paths()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "tick":
        tick(p)
    elif cmd == "status":
        status(p)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
