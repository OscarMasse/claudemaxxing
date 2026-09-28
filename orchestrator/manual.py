#!/usr/bin/env python3
"""Manual batch launch: the owner runs the backlog when HE decides.

  manual.py --for 5h            behave like a night for that long
  manual.py --count 6           launch the next 6 tasks of the queue
  manual.py --tasks a b.md ...  launch exactly these tasks, in this order

Options: --account NAME, --parallel N, --fable-slots N, --budget USD,
--slice MIN, --dry-run (print the plan, launch nothing), --check (validate
the arguments and exit; manual.sh runs it before detaching).

The gatekeeper's pacing rules (night window, morning guard, the night's
budget share, the activity lock, "a lost night stays lost") exist so that
UNATTENDED runs never overflow. They bind the system, not the owner
(README decision log, 2026-09-26), so none of them is consulted here.
What stays is what protects the machine and the account whoever launches:
the parallel-slot ceiling, the Fable pacing slots, the per-session cost cap
(run.sh), RUNNING locks, models the account has refused (lib/quota.py),
task claims, prerequisites, and the PAUSED kill switch - which the stall
detectors also set on their own, so a manual run refuses to start under it
rather than launching into whatever tripped it.

Selection is the gatekeeper's own (tasks.launch_order, tasks.resolve) and
every launch goes through gate.record_launch and run.sh, so a manual session
leaves the same claims, ledger rows and digest lines as a night one, tagged
`manual`. Power source is deliberately not a condition: manual.sh runs this
loop under the platform keep-awake hook, which holds on battery too.
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

ORCH = Path(__file__).resolve().parent
sys.path.insert(0, str(ORCH))
import gate  # noqa: E402
from lib import config, ledger, quota, tasks  # noqa: E402

TICK_S = 60  # manual runs refill faster than the 5-min night tick; run.sh
# takes its lock milliseconds after start, far inside this interval
DRY_RUN_QUEUE = 100  # how deep --dry-run projects an unbounded queue mode


def parse_duration(raw):
    """`5h`, `90m`, `1h30m` -> timedelta. Anything else raises ValueError."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", raw.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"not a duration: {raw!r} (use e.g. 5h, 90m, 1h30m)")
    return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))


class Plan:
    """What the owner asked for, and what is left of it."""

    def __init__(self, mode, slice_min, parallel, fable_slots, deadline=None,
                 count=None, refs=None, budget=None):
        self.mode = mode  # "for" | "count" | "tasks"
        self.slice_min = slice_min
        self.parallel = min(parallel, gate.MAX_SLOTS)
        self.fable_slots = fable_slots
        self.deadline = deadline
        self.count_left = count
        self.refs = list(refs or [])
        self.budget_left = budget
        self.done = None  # why the run stopped launching, once it has

    def describe(self):
        what = {"for": f"until {self.deadline and self.deadline.strftime('%F %T')}",
                "count": f"count={self.count_left}",
                "tasks": f"tasks={','.join(tasks.task_name(r) for r in self.refs)}"}
        budget = "-" if self.budget_left is None else f"${self.budget_left:.2f}"
        return (f"mode={self.mode} {what[self.mode]} parallel={self.parallel} "
                f"fable_slots={self.fable_slots} slice={self.slice_min} "
                f"budget={budget}")


def _explicit_candidates(p, acct, projs, plan):
    """The listed tasks launchable now, in list order. Drops (and reports)
    a task that can never launch as it stands; keeps one waiting on
    prerequisites that are still coming: listed later, or in progress.
    Returns (candidates, [(name, why dropped)])."""
    root, name = p["root"], acct["name"]
    listed = {tasks.task_name(r) for r in plan.refs}
    cands, dropped = [], []
    for ref in list(plan.refs):
        t, unmet, reason = tasks.resolve(root, projs, name, ref)
        if t is not None:
            cands.append(t)
            continue
        if unmet:
            if _coming(root, unmet, listed):
                continue  # waits for its prerequisites
            reason = f"prerequisites not done and not coming: {' '.join(unmet)}"
        plan.refs.remove(ref)
        dropped.append((tasks.task_name(ref), reason))
    return cands, dropped


def _coming(root, unmet, listed):
    """Whether every unmet prerequisite can still get done during this run:
    listed itself, or already being worked on."""
    return all(u in listed or _status(root, u) == "in-progress" for u in unmet)


def _status(root, name):
    path = tasks.task_path(root, name)
    return tasks._frontmatter(path).get("status") if path.is_file() else None


def select(p, acct, projs, plan, now):
    """Sessions to launch on this tick, cost estimate attached. Consumes the
    plan (count, list, budget) for what it returns."""
    name = acct["name"]
    state = p["state"] / name
    free = plan.parallel - gate.active_slots(p, state)
    if free <= 0:
        return []
    # Same model pacing as the night tick (gate.tick_account): refused models
    # get no slot, Fable gets what the running Fable sessions leave.
    model_slots = {family: 0 for family in quota.blocked(state, now)}
    model_slots.setdefault("fable", max(
        plan.fable_slots - gate.running_models(state).count("fable"), 0))
    if plan.mode == "tasks":
        cands, dropped = _explicit_candidates(p, acct, projs, plan)
        for task_name, why in dropped:
            gate.log(p, f"account={name} manual dropped task={task_name}: {why}")
        cands = tasks.launchable(cands, free, model_slots)
    else:
        count = free if plan.count_left is None else min(free, plan.count_left)
        cands = tasks.launch_order(p["root"], projs, name, count=count,
                                   done=gate.duties_served(state),
                                   period_keys=gate.period_keys(acct, now),
                                   model_slots=model_slots,
                                   today=now.date())
    measured = ledger.session_costs(state)
    default_cost = float(acct.get("est_session_usd", 2.85))
    picked = []
    for cand in cands:
        est = measured.get((cand["path"], cand["model"]), default_cost)
        if plan.budget_left is not None and est > plan.budget_left:
            plan.done = f"budget left ${plan.budget_left:.2f} < next estimate ${est:.2f}"
            break
        cand["est_usd"] = float(est)
        picked.append(cand)
        if plan.budget_left is not None:
            plan.budget_left -= est
    for t in picked:
        if plan.count_left is not None:
            plan.count_left -= 1
        if plan.mode == "tasks":
            plan.refs = [r for r in plan.refs
                         if tasks.task_name(r) != Path(t["path"]).stem]
    return picked


def launch(p, acct, plan, t):
    """Start one run.sh session, detached from this runner: stopping the
    runner stops the launches, never a session already working."""
    state_root = p["state"]
    state_root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, ORCH_LAUNCH="manual", BACKLOG_ROOT=str(p["root"]))
    with open(state_root / "runs.out", "a") as out:
        subprocess.Popen(
            [str(ORCH / "run.sh"), "--account", acct["name"], str(plan.slice_min),
             t["path"], t["model"], t["effort"], t["project"],
             f"{t['est_usd']:.2f}", t["delivery"]],
            cwd=ORCH, env=env, stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.STDOUT, start_new_session=True)


def run(p, acct, projs, plan, clock, sleep, start=launch, stopped=lambda: False):
    """The loop: tick, launch, sleep, until the plan is spent. Returns why it
    stopped. `clock`, `sleep` and `start` are injected for the tests."""
    name = acct["name"]
    state = p["state"] / name
    gate.log(p, f"account={name} manual start pid={os.getpid()} {plan.describe()}")
    launched = 0
    while plan.done is None:
        now = clock()
        if stopped():
            plan.done = "stopped by the owner"
        elif p["paused"].exists():
            plan.done = "kill switch set (orchestrator/PAUSED)"
        elif plan.deadline is not None and now >= plan.deadline:
            plan.done = "duration elapsed"
        if plan.done:
            break
        picked = select(p, acct, projs, plan, now)
        for t in picked:
            # Duties and fillers named by the owner do not consume their
            # period: asking for one by hand must not cost tonight's run of it.
            gate.record_launch(p, state, t, now, plan.slice_min,
                               pace_duty=plan.mode != "tasks")
            start(p, acct, plan, t)
            launched += 1
            gate.log(p, f"account={name} manual launch task={Path(t['path']).name} "
                        f"model={t['model']} est=${t['est_usd']:.2f}")
        if plan.done:
            break
        if plan.count_left == 0:
            plan.done = "count reached"
        elif plan.mode == "tasks" and not plan.refs:
            plan.done = "task list launched" if launched else "nothing launched"
        elif not picked and gate.active_slots(p, state) == 0:
            # Nothing launchable and nothing running that could change that
            # (a finishing session is what unblocks a prerequisite).
            plan.done = "nothing left to launch"
        if plan.done:
            break
        sleep(TICK_S)
    left = f" not launched={','.join(tasks.task_name(r) for r in plan.refs)}" \
        if plan.refs else ""
    gate.log(p, f"account={name} manual end launched={launched} "
                f"reason={plan.done!r}{left}")
    return plan.done


def dry_run(p, acct, projs, plan, now):
    """Print what the plan would launch, in order, without launching. Slots
    and Fable pacing are left out: they decide WHEN, not WHAT."""
    name = acct["name"]
    state = p["state"] / name
    measured = ledger.session_costs(state)
    default_cost = float(acct.get("est_session_usd", 2.85))
    print(f"account={name} backlog={p['root']} {plan.describe()}")
    if plan.mode == "tasks":
        rows = []
        listed = {tasks.task_name(r) for r in plan.refs}
        for ref in plan.refs:
            t, unmet, reason = tasks.resolve(p["root"], projs, name, ref)
            if t is None:
                if unmet and _coming(p["root"], unmet, listed):
                    why = f"launches once done: {' '.join(unmet)}"
                elif unmet:
                    why = f"prerequisites not done and not coming: {' '.join(unmet)}"
                else:
                    why = reason
                print(f"  SKIP {tasks.task_name(ref)}: {why}")
            else:
                rows.append(t)
    else:
        depth = plan.count_left if plan.count_left is not None else DRY_RUN_QUEUE
        rows = tasks.launch_order(p["root"], projs, name, count=depth,
                                  done=gate.duties_served(state),
                                  period_keys=gate.period_keys(acct, now),
                                  model_slots={}, today=now.date())
    cum, budget = 0.0, plan.budget_left
    for t in rows:
        est = measured.get((t["path"], t["model"]), default_cost)
        if budget is not None and cum + est > budget:
            print(f"  -- budget ${budget:.2f} reached")
            break
        cum += est
        print(f"  {t['model']:6} est=${est:5.2f} cum=${cum:6.2f} "
              f"{t['project']:13} {Path(t['path']).name}")


def build_plan(args, acct, now):
    if args.parallel is not None and args.parallel < 1:
        raise ValueError("--parallel must be at least 1")
    parallel = args.parallel if args.parallel is not None else int(
        acct.get("max_parallel_sessions", 2))
    fable = args.fable_slots if args.fable_slots is not None else int(
        acct.get("max_fable_slots", 1))
    slice_min = args.slice or int(acct.get("night_slice_min", 50))
    common = dict(slice_min=slice_min, parallel=parallel, fable_slots=fable,
                  budget=args.budget)
    if args.duration:
        return Plan("for", deadline=now + parse_duration(args.duration), **common)
    if args.count is not None:
        if args.count < 1:
            raise ValueError("--count must be at least 1")
        return Plan("count", count=args.count, **common)
    return Plan("tasks", refs=args.tasks, **common)


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="manual.sh", description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--for", dest="duration", metavar="DURATION")
    mode.add_argument("--count", type=int)
    mode.add_argument("--tasks", nargs="+", metavar="TASK")
    ap.add_argument("--account")
    ap.add_argument("--parallel", type=int)
    ap.add_argument("--fable-slots", type=int)
    ap.add_argument("--budget", type=float, metavar="USD")
    ap.add_argument("--slice", type=int, metavar="MIN")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    return ap.parse_args(argv)


def pick_account(cfg, wanted):
    accts = config.accounts(cfg)
    if wanted is None:
        return accts[0]
    for a in accts:
        if a["name"] == wanted:
            return a
    raise ValueError(f"no account named {wanted!r}")


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        p = gate.paths()
    except config.BacklogRootError as e:
        print(f"manual: {e}", file=sys.stderr)
        return 2
    print(f"backlog={p['root']}")
    cfg = config.load(p["config"])
    projs = config.projects(cfg)
    try:
        acct = pick_account(cfg, args.account)
        problem = config.misconfigured_account(acct)
        if problem:
            raise ValueError(f"account {acct['name']} misconfigured: {problem}")
        now = gate.now_from_env(acct)
        plan = build_plan(args, acct, now)
    except ValueError as e:
        print(f"manual: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        dry_run(p, acct, projs, plan, now)
        return 0
    if p["paused"].exists():
        print(f"manual: kill switch set, remove {p['paused']} to launch "
              f"(the stall detectors set it too: check gatekeeper.log first)",
              file=sys.stderr)
        return 1
    if plan.mode == "tasks":
        # Checked in the foreground: once detached, a typo or a wrong backlog
        # root would only reach a log (2026-09-27: every task of a list was
        # dropped as "no such task" because BACKLOG_ROOT was unset, and the
        # runner still reported the list as launched).
        missing = [tasks.task_name(r) for r in plan.refs
                   if tasks.resolve(p["root"], projs, acct["name"], r)[2] == "no such task"]
        if missing:
            print(f"manual: no such task in {p['root'] / 'tasks'}: {', '.join(missing)}",
                  file=sys.stderr)
            return 2
    if args.check:
        return 0
    stop = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.append(True))
    marker = p["state"] / acct["name"] / f"MANUAL.{os.getpid()}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(plan.describe() + "\n")
    try:
        reason = run(p, acct, projs, plan,
                     clock=lambda: gate.now_from_env(acct),
                     sleep=lambda s: _interruptible_sleep(s, stop),
                     stopped=lambda: bool(stop))
    finally:
        marker.unlink(missing_ok=True)
    print(f"manual: done ({reason})")
    return 0


def _interruptible_sleep(seconds, stop):
    end = time.monotonic() + seconds
    while not stop and time.monotonic() < end:
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
