#!/usr/bin/env python3
"""Gatekeeper decision CLI. Zero Claude tokens: pure local reads.

  gate.py tick    -> one decision line PER ACCOUNT, logs each decision:
                       RUN <account> <slice_min> <task> <model> <effort> <project>
                       SKIP <account> <reason>
                     (plus a global "SKIP paused" when the kill switch is set)
  gate.py status  -> human-readable per-account quota summary for the digest
  gate.py plan    -> dry projection of the remaining nights' token allocations

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
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import activity, config, controller, ledger, tasks, usage  # noqa: E402

LOCK_TTL_S = 4.5 * 3600
MAX_SLOTS = 8  # run.sh allocates RUNNING.1..8; a higher ceiling cannot be used
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


def night_start_dt(acct, now):
    """Tonight's night_start as an aware datetime, or None outside the night.

    The night window does not cross midnight (see config `night_start` /
    `night_end`), so the occurrence in progress is always today's."""
    def at(hhmm):
        h, m = str(hhmm).split(":")
        return now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    start = at(acct["night_start"])
    return start if start <= now < at(acct["night_end"]) else None


def spent_tonight(state, acct, now):
    """Tokens this night has already burned, for the night allocator."""
    start = night_start_dt(acct, now)
    return ledger.spent_since(state, start) if start else 0


def period_keys(acct, now):
    """Identifier of the period in progress, per duty period.

    A duty is due when its recorded key differs from the current one, so these
    keys are what makes "once per night" mean once per night and not once per
    tick. `nightly` is absent outside the night window: a nightly duty is not
    schedulable then.
    """
    local = now.astimezone(ZoneInfo(acct["reset_tz"]))
    keys = {"daily": local.date().isoformat(),
            "weekly": controller.prev_reset(acct, now).date().isoformat()}
    start = night_start_dt(acct, now)
    if start:
        keys["nightly"] = start.astimezone(ZoneInfo(acct["reset_tz"])).date().isoformat()
    return keys


def duties_served(state):
    """task path -> period key last launched for it."""
    f = state / "duties.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except (ValueError, OSError):
        return {}


def record_duty(state, path, key):
    """Mark a duty served for this period. Written at LAUNCH, not on success: a
    failing duty must not relaunch every tick until the period ends."""
    served = duties_served(state)
    served[path] = key
    state.mkdir(parents=True, exist_ok=True)
    tmp = state / "duties.json.tmp"
    tmp.write_text(json.dumps(served, indent=2, sort_keys=True))
    tmp.replace(state / "duties.json")


def tick_account(p, acct, projs):
    """One scheduling decision for one account. Returns the account's idle
    minutes (for the presence-gated notifications)."""
    name = acct["name"]
    now = now_from_env(acct)
    idle = idle_for_account(p["root"], acct)
    snap = usage.snapshot(acct, now)
    state = p["state"] / name
    # The night allocator needs to know what tonight already cost, so its
    # budget is a remainder rather than a fresh grant on every tick.
    snap["spent_tonight"] = spent_tonight(state, acct, now)
    d = controller.decide(acct, now, snap, idle)
    # Parallel slots. At night and in the burn-down the count is decided by the
    # budget alone (see the fill loop below): the real metric is tokens, not a
    # task count, so a night may be one heavy session or many small ones.
    # `max_parallel_sessions` is a SAFETY ceiling, not a pacing tool - it
    # protects the machine (each session may run docker builds, test suites and
    # Playwright) and bounds how many sessions can contend on the same repos.
    # run.sh only allocates RUNNING.1..8 lock slots, so 8 is the hard structural
    # limit however high this is set. Daytime stays on the conservative
    # `day_parallel` cap: that regime exists to nibble a surplus, not to fill it.
    if d.regime in ("night", "prereset"):
        cap_slots = min(int(acct.get("max_parallel_sessions", 4)), MAX_SLOTS)
    else:
        cap_slots = int(acct.get("day_parallel", 1))
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
        keys = period_keys(acct, now)
        candidates = tasks.launch_order(p["root"], projs, name,
                                        max_model=max_floor, count=free,
                                        done=duties_served(state),
                                        period_keys=keys)
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
            # Budget rules per scheduling class. A duty is mandatory: charged
            # to the budget, never gated by it. A queue task is exempt when it
            # is the tick's first session, or a task heavier than a whole night
            # could never start at all. A filler is pure surplus: no exemption,
            # it only ever runs on budget that is already there.
            exempt = cand["sched"] == "duty" or (
                not picked and cand["sched"] != "filler")
            if not exempt and est_burn > budget:
                break
            cand["est_tokens"] = int(est_burn)
            picked.append(cand)
            budget -= est_burn
    log(p, f"account={name} {d.action} reason={d.reason!r} slice={d.slice_min} "
           f"regime={d.regime} model={d.model} week={snap['week_tokens']} "
           f"idle={idle} free_slots={free} "
           f"duties={[t['path'] for t in picked if t['sched'] == 'duty'] or '-'} "
           f"fillers={[t['path'] for t in picked if t['sched'] == 'filler'] or '-'} "
           f"tasks={[t['path'] for t in picked if not t['sched']] or '-'}")
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
                  f"{t['effort']} {t['project']} {t.get('est_tokens', 0)}")
            if t["sched"] == "duty":
                record_duty(state, t["path"], t["period_key"])
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
        available = controller.surplus(acct, now, snap["week_tokens"])
        print(f"account={name}")
        print(f"week_tokens={snap['week_tokens']}")
        print(f"cap={cap:.0f} reserve={reserve:.0f} available={available:.0f}")
        print(f"next_reset={reset.isoformat()} days_remaining={days:.2f}")
        print(f"idle_min={idle} promo_until={acct['promo_until']}")
        for task_name, unmet in tasks.blocked(p["root"], projs, name):
            print(f"blocked task={task_name} unmet={' '.join(unmet)}")
        # Tonight's allocation, so the digest can see the plan, not just the cap.
        # Outside the night, the upcoming one is already in _nights_remaining;
        # counting it twice would understate every allocation by a factor r.
        in_night = night_start_dt(acct, now) is not None
        tonight = controller.night_budget(acct, now, available,
                                          spent_tonight(p["state"] / name, acct, now),
                                          in_night=in_night)
        print(f"night_budget={tonight:.0f} "
              f"nights_remaining={controller.nights_remaining(acct, now, in_night)}")
        keys = period_keys(acct, now)
        served = duties_served(p["state"] / name)
        for path, period, supported in tasks.duties(p["root"], projs, name):
            due = supported and served.get(path) != keys.get(period)
            print(f"duty task={Path(path).name} period={period} "
                  f"supported={'yes' if supported else 'NO'} "
                  f"due={'yes' if due else 'no'}")
        for t in tasks.fillers(p["root"], projs, name, "fable"):
            print(f"filler task={Path(t['path']).name}")
        for task, a in sorted(ledger.accuracy(p["state"] / name).items()):
            print(f"estimate task={task} runs={a['runs']} est={a['est_tokens']} "
                  f"actual={a['actual_tokens']} ratio={a['ratio']:.2f}")
        costs = ledger.stats(p["state"] / name)
        for task, s in sorted(costs.items()):
            print(f"cost task={task} runs={s['runs']} usd={s['cost_usd']:.2f} "
                  f"out_tokens={s['out_tokens']} total_tokens={s['total_tokens']}")
    # Printed once, after the accounts: an orphaned task belongs to none of them.
    for task_name, project in tasks.orphaned(p["root"], projs):
        print(f"orphaned task={task_name} project={project}")


def plan(p):
    """Dry projection of the nights left before the reset, per account.

    Answers "where is my quota going?" without launching anything and without
    waiting for 02:00: for each remaining night it prints the allocation the
    controller would grant, assuming nothing else consumes quota in between.
    Reality will differ (that is the point of recomputing every tick), but the
    SHAPE of the plan - back-loaded, open bar on the last night - is visible
    here, and so is a misconfigured `night_budget_ratio`.
    """
    cfg = config.load(p["config"])
    for acct in config.accounts(cfg):
        name = acct["name"]
        now = now_from_env(acct)
        snap = usage.snapshot(acct, now)
        reset = controller.next_reset(acct, now)
        left = controller.nights_remaining(acct, now, night_start_dt(acct, now) is not None)
        print(f"account={name} nights_remaining={left} reset={reset.isoformat()}")
        pool = None
        # Walk the nights from the current one to the last, keeping the pool the
        # allocator would see after each night spent its share.
        for i in range(left, 0, -1):
            probe = reset - timedelta(days=i - 1, hours=3)
            if pool is None:
                pool = controller.surplus(acct, probe, snap["week_tokens"])
            share = controller.night_budget(acct, probe, pool, 0, in_night=True)
            print(f"  night {probe.date().isoformat()} nights_left={i} "
                  f"budget={share:.0f} pool={max(pool, 0.0):.0f}")
            pool -= share


def main():
    p = paths()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "tick":
        tick(p)
    elif cmd == "status":
        status(p)
    elif cmd == "plan":
        plan(p)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
