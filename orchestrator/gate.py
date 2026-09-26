#!/usr/bin/env python3
"""Gatekeeper decision CLI. Zero Claude tokens: pure local reads.

  gate.py tick    -> one decision line PER ACCOUNT, logs each decision:
                       RUN <account> <slice_min> <task> <model> <effort> <project>
                       SKIP <account> <reason>
                     (plus a global "SKIP paused" when the kill switch is set)
  gate.py status  -> human-readable per-account quota summary for the digest
  gate.py plan    -> dry projection of the remaining nights' USD allocations

Every budget figure is USD at Anthropic list price (lib/transcripts.py), the
weighting the account's limit applies. Money is printed with two decimals.

Every account is scheduled independently: its own quota budget, its own
activity lock (idle = interactive use of THAT account's Claude profile), its
own parallel-slot ceilings, and its own state under state/<account>/. Two
accounts can both launch sessions in the same tick.

Env overrides (tests / manual runs):
  ORCH_ROOT (backlog root override, wins over BACKLOG_ROOT), BACKLOG_ROOT,
  ORCH_CONFIG (explicit config file, wins over $BACKLOG_ROOT/config.yaml and
  the repo default; see lib/config.resolve_path),
  ORCH_NOW (ISO), ORCH_IDLE_MIN, ORCH_IDLE_MIN_<ACCOUNT>,
  ORCH_USAGE_JSON, ORCH_USAGE_JSON_<ACCOUNT>, ORCH_NO_NOTIFY,
  ORCH_PLATFORM, ORCH_DOCKER_BIN (empty disables the janitor)
  (<ACCOUNT> = name upper-cased, non-alphanumerics -> _)
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
from lib import (activity, config, controller, janitor, ledger, quota,  # noqa: E402
                 ratelimits, stalls, tasks, usage)

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


def regime_key(acct, regime, key, default):
    """`prereset_<key>` in the burn-down when set, `<key>` otherwise."""
    if regime == "prereset" and f"prereset_{key}" in acct:
        return acct[f"prereset_{key}"]
    return acct.get(key, default)


def idle_for_account(acct):
    """Idle minutes on THIS account's profile: interactive use of one
    subscription never blocks background work on another."""
    raw = (os.environ.get(f"ORCH_IDLE_MIN_{usage.env_name(acct['name'])}")
           or os.environ.get("ORCH_IDLE_MIN"))
    if raw is not None:
        return float(raw)
    # Claude Code stores transcripts under <config dir>/projects/<path-encoded-cwd>.
    # The headless sessions' project dir is excluded: they must not count as
    # the owner's activity. run.sh cds into this directory before launching
    # them, so that is their cwd - not the backlog root, where the owner's own
    # interactive sessions run and must keep counting.
    exclude = str(Path(__file__).resolve().parent).replace("/", "-")
    return activity.idle_minutes(Path(acct["claude_config_dir"]) / "projects",
                                 exclude, time.time())


def log(p, msg):
    p["state"].mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%F %T")
    with open(p["log"], "a") as f:
        f.write(f"{stamp} {msg}\n")


def notify_duty(p, idle, open_cmd=None):
    """One user notification per new unchecked question, only when the owner
    is present. Delivery goes through the platform notify.sh hook; a missing
    hook is not an error, the question still lands in NEEDS-HUMAN.md.

    The hook is handed NEEDS-HUMAN.md so the notification can open it: the
    question is a checkbox to answer and tick, and a notification that cannot
    take the owner to it makes them hunt for the file. `open_cmd` is passed
    through as ORCH_NOTIFY_OPEN (`notify_open_cmd` in config.yaml).
    """
    if idle is None or idle > PRESENT_MIN or not p["needs"].exists():
        return
    hook = Path(__file__).resolve().parent / "platform" / platform_name() / "notify.sh"
    env = dict(os.environ)
    if open_cmd:
        env["ORCH_NOTIFY_OPEN"] = str(open_cmd)
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
            subprocess.run([str(hook), "Backlog needs you", question[:120],
                            str(p["needs"])], capture_output=True, env=env)
        p["state"].mkdir(parents=True, exist_ok=True)
        with open(p["notified"], "a") as f:
            f.write(h + "\n")


def _fresh_locks(state_dir):
    """(lock path, fields) of every RUNNING* lock in one account's state dir
    that is younger than the lock TTL. The fields are what run.sh wrote:
    `<pid> <epoch> [<model>]`; the model is absent in digest runs and in
    locks written before it was recorded. A lock that cannot be read is
    yielded with an empty field list, so the caller can break it."""
    if not state_dir.is_dir():
        return [], []
    fresh, stale = [], []
    for lock in sorted(state_dir.glob("RUNNING*")):
        try:
            fields = lock.read_text().split()
            started = float(fields[1])
        except (IndexError, ValueError, OSError):
            fields, started = [], 0.0
        (fresh if time.time() - started < LOCK_TTL_S else stale).append((lock, fields))
    return fresh, stale


def active_slots(p, state_dir):
    """Count fresh RUNNING* locks in one account's state dir; break stale ones."""
    fresh, stale = _fresh_locks(state_dir)
    for lock, _ in stale:
        lock.unlink(missing_ok=True)
        log(p, f"broke stale lock {state_dir.name}/{lock.name}")
    return len(fresh)


def running_models(state_dir):
    """Model family of each live session, from the third field of its lock.

    `max_fable_slots` is about concurrent Fable SESSIONS, and a session lives
    across many ticks: on 2026-09-12 a tick counting only its own launches put
    a second Fable session next to one launched five minutes earlier, which is
    exactly the race the setting exists to prevent. Locks without a model
    field (digest runs, older launchers) hold a slot but no model slot."""
    fresh, _ = _fresh_locks(state_dir)
    return [fields[2] for _, fields in fresh if len(fields) > 2]


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
    """USD this night has already burned, for the night allocator."""
    start = night_start_dt(acct, now)
    return ledger.spent_since(state, start) if start else 0


def period_keys(acct, now):
    """Identifier of the period in progress, per duty period.

    A duty is due when its recorded key differs from the current one, so these
    keys are what makes "once per night" mean once per night and not once per
    tick.

    A period is ABSENT when the current tick is outside its window, which is
    what makes a nightly duty night-only: no key, not due. `weekly` is keyed on
    the quota week (the last reset), so it is always present.

    `filler` is the night a tick belongs to (the local date 12 hours earlier,
    so an evening burn-down and the 04:00 tick share one): a filler runs at
    most once per night. Without it a filler was relaunched on every tick the
    queue ran dry - 42 sessions of the same no-op chore on the 2026-09-24
    burn-down, where the budget is unlimited and never stopped it.
    """
    tz = ZoneInfo(acct["reset_tz"])
    keys = {"weekly": controller.prev_reset(acct, now).date().isoformat(),
            "filler": (now.astimezone(tz) - timedelta(hours=12)).date().isoformat()}
    start = night_start_dt(acct, now)
    if start:
        keys["nightly"] = start.astimezone(tz).date().isoformat()
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


UNCALIBRATED = ("no rate-limit history to derive caps from: seed it once with "
                "`lib/ratelimits.py seed`")


def calibrated(p, acct, now):
    """(account with its derived caps merged in, the derivation), or (None,
    None) when the history has no seed. The controller stays pure: it reads
    the caps off the account dict like any other key."""
    derived = ratelimits.caps(ratelimits.load(p["state"] / acct["name"]), now,
                              acct["reset_tz"])
    if derived is None:
        return None, None
    merged = dict(acct, **{k: derived[k] for k in
                           ("weekly_cap_usd", "window_cap_usd", "p90_daily_usd")})
    return merged, derived


def tick_account(p, acct, projs):
    """One scheduling decision for one account. Returns the account's idle
    minutes (for the presence-gated notifications)."""
    name = acct["name"]
    # A budget in a unit the engine no longer has cannot be converted, only
    # refused: the whole point of the USD unit is that tokens weigh
    # differently per model, so no factor turns the old number into the new.
    problem = config.misconfigured_account(acct)
    if problem:
        log(p, f"account={name} misconfigured: {problem}")
        print(f"SKIP {name} misconfigured: {problem}")
        return None
    now = now_from_env(acct)
    acct, _derived = calibrated(p, acct, now)
    if acct is None:
        log(p, f"account={name} uncalibrated: {UNCALIBRATED}")
        print(f"SKIP {name} uncalibrated")
        return None
    idle = idle_for_account(acct)
    snap = usage.snapshot(acct, now)
    state = p["state"] / name
    # The night allocator needs to know what tonight already cost, so its
    # budget is a remainder rather than a fresh grant on every tick.
    snap["spent_tonight"] = spent_tonight(state, acct, now)
    d = controller.decide(acct, now, snap, idle)
    # Parallel slots. At night and in the burn-down the count is decided by the
    # budget alone (see the fill loop below): the real metric is dollars, not a
    # task count, so a night may be one heavy session or many small ones.
    # `max_parallel_sessions` is a SAFETY ceiling, not a pacing tool - it
    # protects the machine (each session may run docker builds, test suites and
    # Playwright) and bounds how many sessions can contend on the same repos.
    # run.sh only allocates RUNNING.1..8 lock slots, so 8 is the hard structural
    # limit however high this is set.
    # The burn-down may run wider (`prereset_max_parallel_sessions`): the
    # ordinary ceiling also paces the 5h window the owner inherits the next
    # morning, which is worth nothing on the last night.
    cap_slots = min(int(regime_key(acct, d.regime, "max_parallel_sessions", 4)),
                    MAX_SLOTS)
    free = cap_slots - active_slots(p, state)
    if d.action == "run" and free <= 0:
        # Logged like any other decision: a silent tick is indistinguishable
        # from a gatekeeper that did not run (2026-09-24, a 15-minute hole in
        # the log that was only four busy slots).
        log(p, f"account={name} skip reason='all slots busy' slice=0 "
               f"regime={d.regime} week=${snap['week_usd']:.2f} "
               f"idle={idle} free_slots=0 duties=- fillers=- tasks=-")
        print(f"SKIP {name} running")
        return idle
    # Task selection happens here (not in the session): the tasks' declared
    # models must be known before launch, because the model is what the cost
    # estimate below is keyed on. That is the only thing the scheduler does
    # with it - a session always runs the model its task declares.
    # Slot count is budget-driven: estimated burn per session (measured rates
    # from the ledger, per-model defaults otherwise) must fit the slice budget.
    # A heavy task that alone consumes the budget gets exactly one session.
    # Models the account has told us it will refuse (lib/quota.py). The night
    # of 2026-09-09 spent three of its last four slots relaunching Fable
    # sessions into a Fable limit that had already answered `You've hit your
    # session limit` at 02:25 - the engine had no memory of being told.
    out_of_quota = quota.blocked(state, now)
    for family, until in sorted(out_of_quota.items()):
        log(p, f"account={name} out of quota model={family} "
               f"until={until.isoformat()}")
    picked = []
    if d.action == "run":
        # Neither the tick nor the regime ranks the models. Two rules used
        # to: a ceiling capped a tick at its own budget-derived model, so on
        # 2026-09-10 a surplus just under the Fable threshold filtered every
        # `model: fable` task out of the very regime meant to run them - five
        # weeks running - and a floor then upgraded whatever did get picked to
        # that same model, burning Fable tokens on work that asked for Sonnet.
        # Both are gone, and with them the ordering that made them expressible:
        # a task's model says what its session runs on, and what the tick can
        # afford is answered once, by the budget, below. Several models in one
        # night is the normal case; `max_fable_slots` paces the one model with
        # its own separate limit.
        keys = period_keys(acct, now)
        # How many of this tick's slots the strongest model may take. It is a
        # pacing tool, unlike max_parallel_sessions: the binding constraint on
        # a Fable night is the account's Fable limit, not wall-clock time, and
        # four Fable sessions racing each other exhaust it in under two hours
        # (measured 2026-09-09), leaving nothing for the rest of the night and
        # no slot for the cheaper models that were nowhere near their own
        # limit. Serialising them costs almost nothing - a night fits only
        # three or four sessions per slot anyway - and keeps the other slots
        # doing useful work. It applies in the pre-reset burn-down as well:
        # the wall it would run into there is the same one.
        # Sessions launched by earlier ticks count too: the constraint is on
        # what runs concurrently, not on what one tick launches.
        fable_slots = int(regime_key(acct, d.regime, "max_fable_slots", 1))
        fable_running = running_models(state).count("fable")
        # The per-model rules go INTO the selection rather than filtering its
        # output: cutting the queue to `free` first and dropping the models
        # that cannot launch second leaves slots empty whenever the queue
        # head is heavy in one model (2026-09-12: opus, fable, fable, sonnet
        # with four free slots launched two, all night).
        model_slots = {family: 0 for family in out_of_quota}
        model_slots.setdefault("fable", max(fable_slots - fable_running, 0))
        candidates = tasks.launch_order(p["root"], projs, name, count=free,
                                        done=duties_served(state),
                                        period_keys=keys,
                                        model_slots=model_slots)
        # What a session costs is a property of the work, not of the slice it
        # was allotted: sessions do not fill their slice (measured median
        # utilisation here: 3%), so both the measured figure and the cold-start
        # default are per SESSION. This used to be a per-minute rate multiplied
        # by the slice, which was accidentally right for a task with ledger
        # history (the divide and the multiply cancelled) and ~30x too high for
        # one without - 400k/min x 50min = 20M against a measured median of
        # 726k. That single wrong number capped most nights at one session,
        # since every candidate after the first was gated on it.
        # One default, not one per model: the measurement says session cost is
        # driven by the task, which is why the learned figure is keyed on
        # (task, model). A per-model prior is not supported by the data here
        # (opus and fable sessions measured CHEAPER than sonnet ones), so
        # inventing three numbers would only look more precise than it is.
        # 2.85 is the p75 of `cost_usd` over the 131 orchestrate sessions in
        # the ledger on 2026-09-12 (median 1.46, p90 4.29, max 13.65).
        measured = ledger.session_costs(state)
        default_cost = float(acct.get("est_session_usd", 2.85))
        budget = d.budget_usd
        for cand in candidates:
            est_burn = measured.get((cand["path"], cand["model"]), default_cost)
            # Budget rules per scheduling class. A duty is mandatory: charged
            # to the budget, never gated by it. A queue task is exempt when it
            # is the tick's first session, or a task heavier than a whole night
            # could never start at all. A filler is pure surplus: no exemption,
            # it only ever runs on budget that is already there.
            exempt = cand["sched"] == "duty" or (
                not picked and cand["sched"] != "filler")
            if not exempt and est_burn > budget:
                break
            cand["est_usd"] = float(est_burn)
            picked.append(cand)
            budget -= est_burn
    log(p, f"account={name} {d.action} reason={d.reason!r} slice={d.slice_min} "
           f"regime={d.regime} week=${snap['week_usd']:.2f} "
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
                  f"{t['effort']} {t['project']} {t.get('est_usd', 0.0):.2f} "
                  f"{t['delivery']}")
            if t["sched"] in ("duty", "filler"):
                record_duty(state, t["path"], t["period_key"])
            elif t["sched"] is None and not t["parallel"]:
                # Queue tasks are claimed here, at launch, or the next tick
                # relaunches them (see tasks.claim). Duties and fillers stay
                # `ready` by contract (duties.json is what paces a duty), and
                # a `parallel: true` task is shared by design: its shards
                # coordinate through claims of their own.
                try:
                    stalls.record_launch(p["state"], t["path"], now)
                except OSError as e:
                    log(p, f"stall detector failed (launch): {e!r}")
                tasks.claim(t["path"], now.strftime("%F"), t["model"],
                            d.slice_min)
    else:
        print(f"SKIP {name} {d.reason}")
    return idle


def janitor_due(p, cfg):
    """Whether this tick may tear down agent-worktree stacks (lib/janitor.py).

    Only when nothing can be using them: no session running on any account
    (the locks do not say which worktree a session works in, so any live
    session protects every stack), inside some account's night window, and
    with the owner away from every account. Daytime is excluded because that
    is when the owner checks an agent's branch by hand in its worktree, stack
    up. Every night starts with nothing running, so a leftover never survives
    into the next night."""
    accts = [a for a in config.accounts(cfg) if not config.misconfigured_account(a)]
    if not accts:
        return False
    if any(active_slots(p, p["state"] / a["name"]) for a in accts):
        return False
    if not any(night_start_dt(a, now_from_env(a)) for a in accts):
        return False
    idles = [idle_for_account(a) for a in accts]
    return all(i is None or i > PRESENT_MIN for i in idles)


def tick(p):
    # Before the kill-switch check: this is what sets it.
    # A detector failure must never cost the tick: it is logged and skipped.
    try:
        tripped = stalls.trip_global(p["state"], p["paused"], p["needs"])
    except Exception as e:  # noqa: BLE001
        tripped = None
        log(p, f"stall detector failed (global): {e!r}")
    if tripped:
        log(p, f"paused: {len(tripped)} consecutive zero-work session failures "
               f"{tripped[0]['ts']}..{tripped[-1]['ts']}")
    if p["paused"].exists():
        print("SKIP paused")
        return
    cfg = config.load(p["config"])
    projs = config.projects(cfg)
    # Logged every tick, not just in `status`: a task the engine refuses to
    # schedule is otherwise indistinguishable from one that is simply not its
    # turn yet, and the whole point of refusing is to be noticed.
    for task_name, problem in tasks.misconfigured(p["root"], projs):
        log(p, f"unschedulable task={task_name}: {problem}")
    # Before any account is scheduled, so a task freed here is eligible in the
    # very same tick rather than waiting 30 minutes for the next one.
    for task_name, hours in tasks.repair_stuck(p["root"], time.time(), LOCK_TTL_S,
                                               datetime.now().strftime("%F")):
        log(p, f"repaired stuck task={task_name} in-progress for {hours:.1f}h")
    # Also before scheduling, so a task that is not advancing is not handed
    # the front of the queue once more.
    try:
        found = stalls.block_detected(p["root"], p["state"], datetime.now(),
                                      tasks.set_status)
    except Exception as e:  # noqa: BLE001
        found = []
        log(p, f"stall detector failed (tasks): {e!r}")
    for task_name, reason, runs, detail in found:
        log(p, f"blocked {reason} task={task_name} runs={runs}: {detail}")
    # Also before scheduling: sessions launched by this tick must start from a
    # machine with no abandoned stacks on it.
    if janitor_due(p, cfg):
        dirs = sorted({d for proj in projs.values() for d in proj["dirs"]})
        for name, ok in janitor.sweep(dirs):
            log(p, f"janitor compose down project={name} ok={ok}")
    idles = []
    for acct in config.accounts(cfg):
        idles.append(tick_account(p, acct, projs))
    known = [i for i in idles if i is not None]
    notify_duty(p, min(known) if known else None, cfg.get("notify_open_cmd"))


def caps_lines(derived, now):
    """The caps in use with their source and age, and the recent limit changes.

    The digest relays these verbatim: a cap from `history` or `seed` is an old
    reading carried forward, and its age says how much to trust it.
    """
    out = []
    for key, d in derived["detail"].items():
        age_h = (now - d["as_of"]).total_seconds() / 3600
        out.append(f"cap {key}={d['cap']:.2f} source={d['source']} "
                   f"as_of={d['as_of'].date().isoformat()} age_h={age_h:.1f}")
    out.append(f"cap p90_daily_usd={derived['p90_daily_usd']:.2f} "
               f"(scaled with weekly_cap_usd)")
    for c in ratelimits.recent_changes(derived, now):
        out.append(f"cap_change {c['cap']} on={c['day']} from={c['from']:.2f} "
                   f"to={c['to']:.2f} ({c['why']})")
    return out


def status(p):
    cfg = config.load(p["config"])
    projs = config.projects(cfg)
    for acct in config.accounts(cfg):
        name = acct["name"]
        print(f"account={name}")
        problem = config.misconfigured_account(acct)
        if problem:
            print(f"account={name} misconfigured: {problem}")
            continue
        now = now_from_env(acct)
        acct, derived = calibrated(p, acct, now)
        if acct is None:
            print(f"account={name} uncalibrated: {UNCALIBRATED}")
            continue
        idle = idle_for_account(acct)
        snap = usage.snapshot(acct, now)
        reset = controller.next_reset(acct, now)
        days = (reset - now).total_seconds() / 86400
        cap = acct["weekly_cap_usd"]
        reserve = acct["p90_daily_usd"] * days
        available = controller.surplus(acct, now, snap["week_usd"])
        print(f"week_usd={snap['week_usd']:.2f}")
        print(f"cap={cap:.2f} reserve={reserve:.2f} available={available:.2f}")
        for line in caps_lines(derived, now):
            print(line)
        # The engine's view of the two `/usage` bars against the derived caps.
        block = snap.get("block")
        window_pct = (f"{block['usd'] / acct['window_cap_usd'] * 100:.1f}"
                      if block and block.get("active") else "none")
        print(f"usage_week_pct={snap['week_usd'] / cap * 100:.1f}")
        print(f"usage_window_pct={window_pct}")
        err = p["state"] / name / ratelimits.ERROR
        if err.is_file():
            print(f"rate_limits_error {err.read_text().strip()}")
        print(f"next_reset={reset.isoformat()} days_remaining={days:.2f}")
        print(f"idle_min={idle}")
        for family, usd in sorted(snap.get("week_by_family", {}).items()):
            print(f"week_model={family} usd={usd:.2f}")
        for family, until in sorted(quota.blocked(p["state"] / name, now).items()):
            print(f"out_of_quota model={family} until={until.isoformat()}")
        for model_id in snap.get("unknown_models", []):
            print(f"unknown_model id={model_id} (counted, family unrecognized)")
        for task_name, unmet in tasks.blocked(p["root"], projs, name):
            print(f"blocked task={task_name} unmet={' '.join(unmet)}")
        # Tonight's allocation, so the digest can see the plan, not just the cap.
        # Outside the night, the upcoming one is already in _nights_remaining;
        # counting it twice would understate every allocation by a factor r.
        in_night = night_start_dt(acct, now) is not None
        tonight = controller.night_budget(acct, now, available,
                                          spent_tonight(p["state"] / name, acct, now),
                                          in_night=in_night)
        print(f"night_budget={tonight:.2f} "
              f"nights_remaining={controller.nights_remaining(acct, now, in_night)}")
        keys = period_keys(acct, now)
        served = duties_served(p["state"] / name)
        for path, period, supported in tasks.duties(p["root"], projs, name):
            due = supported and served.get(path) != keys.get(period)
            print(f"duty task={Path(path).name} period={period} "
                  f"supported={'yes' if supported else 'NO'} "
                  f"due={'yes' if due else 'no'}")
        for t in tasks.fillers(p["root"], projs, name):
            print(f"filler task={Path(t['path']).name}")
        for task, a in sorted(ledger.accuracy(p["state"] / name).items()):
            ratio = f"{a['ratio']:.2f}" if a["ratio"] is not None else "none"
            print(f"estimate task={task} runs={a['runs']} est_usd={a['est_usd']:.2f} "
                  f"actual_usd={a['actual_usd']:.2f} ratio={ratio}")
        costs = ledger.stats(p["state"] / name)
        for task, s in sorted(costs.items()):
            print(f"cost task={task} runs={s['runs']} usd={s['cost_usd']:.2f} "
                  f"out_tokens={s['out_tokens']} total_tokens={s['total_tokens']}")
    # Printed once, after the accounts: the detector reads every account's runs.
    for task_name, reason, runs, detail in stalls.detect(p["root"], p["state"], datetime.now()):
        print(f"stalled task={task_name} reason={reason} runs={runs} ({detail})")
    for h in stalls.history(p["state"]):
        print(f"detector_blocked task={h['task']} at={h['ts']} "
              f"reason={h['reason']} runs={h['runs']} ({h['detail']})")
    # Printed once, after the accounts: these tasks belong to none of them.
    for task_name, project in tasks.orphaned(p["root"], projs):
        print(f"orphaned task={task_name} project={project}")
    for task_name, problem in tasks.misconfigured(p["root"], projs):
        print(f"misconfigured task={task_name} {problem}")


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
        problem = config.misconfigured_account(acct)
        if problem:
            print(f"account={name} misconfigured: {problem}")
            continue
        now = now_from_env(acct)
        acct, _derived = calibrated(p, acct, now)
        if acct is None:
            print(f"account={name} uncalibrated: {UNCALIBRATED}")
            continue
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
                pool = controller.surplus(acct, probe, snap["week_usd"])
            share = controller.night_budget(acct, probe, pool, 0, in_night=True)
            print(f"  night {probe.date().isoformat()} nights_left={i} "
                  f"budget={share:.2f} pool={max(pool, 0.0):.2f}")
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
