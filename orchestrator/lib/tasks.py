"""Select the next eligible ready tasks for one account from the backlog.

Selection is deterministic and happens in the gatekeeper (not in the session),
because the task's declared model must be known before launching the session.
Frontmatter keys honored: status, project, delivery (branch|pr|local, REQUIRED),
priority, created, due (YYYY-MM-DD), model (sonnet|opus|fable, default sonnet),
effort (low|medium|high, default low), workdir (main, default a worktree),
prerequisites (space-separated task basenames, `.md` suffix optional).

Only tasks/*.md is ever scheduled. Done tasks move to tasks/archive/ (see
`archive_done`); lookups by name resolve both places (see `task_path`).

Eligibility: `status: ready`, and the task's `project:` must exist in the
config's project registry AND belong to the account currently being scheduled.
A task without a `project:` key routes to the project named "default" when one
exists (the legacy single-account fallback synthesizes it). The tool imposes
no taxonomy beyond that: projects are whatever the config declares. A ready task
whose project does not resolve can never be scheduled; `orphaned` reports those
so a typo or a renamed project does not silently swallow work.

Ordering within an account's queue, by class of service (Kanban), then
lexicographically inside each class:

  1. expedite - every task of a project declaring `class: expedite` (work),
     by effective task priority, then project `rank`, then age;
  2. fixed date, due soon - tasks whose (effective) `due:` deadline is close,
     earliest deadline first (EDF), then effective priority, rank, age;
  3. standard - everything else, by effective task priority
     (high/medium/low), then project `rank` (lower preferred), then oldest
     `created`, then filename.

The project only breaks ties: a `high` task of the lowest-ranked project
launches before a `medium` one of the best-ranked. It used to be the other
way round (a project `priority` as the first sort key), so every task of one
project, even a `low` one, starved every other project. A weighted score
(priority weight times project weight, as in WSJF) was rejected for the same
reason: a heavy enough project weight overrides task priority, which is
exactly what the owner ruled out. Aging, the classic cure for starvation of
`low` tasks, is only worth adding once measurement shows them starving.

Deadlines (`due: YYYY-MM-DD`, the Kanban fixed-date class): a dated task keeps
its standard place until the nights left before its deadline get fewer than
the nights its chain needs plus one of margin, then it jumps ahead of
everything but expedite. The chain is the task itself plus every unfinished
task that transitively depends on it and must run after it: a prerequisite
inherits the EARLIEST deadline of its dependents (the blocker of a dated task
is promoted, not the blocked task) and starts early enough for the whole
chain to fit, which is backward scheduling from the deadline as in
critical-path planning. A task needs one night by default, or
ceil(estimated runs) once the caller knows better (`est_runs`). A past
deadline, or a chain that cannot fit or waits on a `blocked` task, is
reported by `deadline_alerts`, never dropped silently. See `_deadlines`.

`delivery` states where the task's output must end up, and it is mandatory:

  `branch` - commit locally on a dedicated branch, never push.
  `pr`     - push the branch and open a pull request. The task is not done
             until the PR exists and its URL is recorded.
  `local`  - nothing leaves the machine (the strictly-local rails).

It is an obligation carried through to the session prompt, not a permission:
leaving it to the session's judgement is what produced the night of
2026-09-09, where four of five tasks stopped at an unpushed branch and one
opened a PR, with nothing in the tasks distinguishing them.

`delivery` REPLACES the former per-task `local_only` key rather than living
next to it: two keys able to describe the same thing (`local_only: true` plus
`delivery: pr`) can contradict each other, and one of them would then have to
win silently. A ready task still carrying `local_only` is unschedulable and
reported by `misconfigured` so the key gets removed rather than obeyed.
The project-level `local_only_default` stays, redefined as a FLOOR instead of
a default: in a local-only project (work), a task declaring `pr` or `branch`
is a contradiction that is reported, never a task quietly downgraded to
`local` nor one quietly allowed to push. A floor cannot supply a default here,
because "no `delivery:` key" is an error in every project.

The declared `model:` is simply the model the session runs on. It is neither a
floor nor a ceiling: no regime upgrades it, none filters a task out for being
too strong, and the scheduler only reads it to estimate what the session will
cost. Ranking the models against each other is what made a `fable` task
unschedulable for weeks at a time, so nothing here orders them any more.
It must name one of `sonnet`, `opus`, `fable` - the engine's own names, not CLI
model ids. Anything else makes the task unschedulable and is reported by
`misconfigured`; see `_declared_model` for why there is no fallback.

Scheduling classes (frontmatter, mutually exclusive):

  `duty: nightly|weekly` - a mandatory routine. Selected BEFORE the priority
  queue and outside it, at most once per period, so it can never be starved by
  a busy project. Its estimated cost comes off the top of the budget.
  There is deliberately no `daily`: it would only have differed from `nightly`
  by also being eligible in the daytime, and the engine no longer runs in the
  daytime at all. Two keys behaving identically is worse than one.

  `filler: true` - an opportunistic routine. Selected only AFTER the priority
  queue has been served and only if budget remains, so it never displaces real
  work, and at most once per night (the `filler` period key, recorded at
  launch like a duty's). Good for open-ended chores that are nice to advance
  but never urgent.

Both are excluded from the normal priority queue; a task declaring neither is
ordinary queued work.

Prerequisite gating: a task with a `prerequisites:` key is not eligible until
every named prerequisite task is itself `status: done`. Only the prerequisite's
own status is read, never its own prerequisites, so this check never recurses
and cycles cannot cause a loop.

Priority inheritance: a blocker is at least as urgent as the most urgent thing
waiting on it. A task's EFFECTIVE priority is the best of its own and of every
unfinished task that transitively depends on it, so a `low` prerequisite sitting
under a `high` task is scheduled as `high`. Without this, declaring a
prerequisite silently deprioritises the very work that unblocks the queue: the
dependent is skipped every tick (unmet prerequisite) while its blocker waits
behind unrelated tasks. Only the effective priority is used for ordering; the
declared `priority:` in the file is never rewritten. See `_effective_priorities`.
"""
import math
import re
from datetime import date
from pathlib import Path

from lib import config

PRIORITY_ORDER = {"high": 0, "medium": 1, "normal": 1, "low": 2}
MODELS = ("sonnet", "opus", "fable")
DELIVERY_VALUES = ("branch", "pr", "local")


def _frontmatter(path):
    """The file's frontmatter keys, or {} when the file vanished since it was
    listed: the gatekeeper's tick may archive it under a concurrent
    `gate.py status` or manual run, which then just skips it."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return {}
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip() and v.strip():
                fm[k.strip()] = v.strip()
    return fm


WORKDIR_MAIN = "main"


def _declared_workdir(fm):
    """Where the task's session works: "worktree" (the default, a dedicated
    git worktree per repo, see lib/workspace.py), "main" when the task
    declares `workdir: main` because it must work in the main checkout, or
    None for any other value, which is reported by misconfigured() and never
    guessed: guessing "worktree" would break the task, guessing "main" would
    reopen the shared-checkout hazard the key exists to close."""
    raw = fm.get("workdir")
    if raw is None:
        return "worktree"
    return raw if raw == WORKDIR_MAIN else None


def task_workdir(path):
    """`_declared_workdir` of the task file at `path`."""
    return _declared_workdir(_frontmatter(Path(path)))


def _declared_model(fm):
    """The task's declared model, or None when it names one the engine does
    not know.

    There is deliberately no fallback. An unrecognized `model:` value used to
    be rewritten to sonnet, silently: five tasks written for Fable ran on
    Sonnet for weeks (they declared the CLI model id `claude-fable-5`, not the
    engine's `fable`) and nothing in any log said so, because the coerced
    value is what every later line printed. A wrong model produces work of the
    wrong quality, which is far more expensive than a task that does not run,
    so the task becomes unschedulable instead and `misconfigured()` reports it.

    The same argument applies to any frontmatter value the engine interprets:
    default when the key is ABSENT, never when it is present and unreadable.
    """
    raw = fm.get("model")
    if raw is None:
        return "sonnet"
    return raw if raw in MODELS else None


def _declared_delivery(fm):
    """The task's declared delivery mode, or None when the key is absent or
    names something the engine does not know.

    There is no default, not even for an absent key - which is stricter than
    `model:`, on purpose. A missing `model:` has one obviously safe reading
    (the cheapest one); a missing `delivery:` has none: guessing `branch`
    hides finished work in a worktree nobody looks at, and guessing `pr`
    publishes something the owner never asked to publish. Both are the owner's
    call, so the task becomes unschedulable and `misconfigured()` reports it.
    """
    raw = fm.get("delivery")
    return raw if raw in DELIVERY_VALUES else None


def _breaches_local_floor(delivery, proj):
    """True when the task's delivery mode breaches its project's local-only
    floor. `local_only_default: true` marks a project whose work must never
    leave the machine (the employer's repos), so `branch` and `pr` are both
    refused there. `branch` is refused too, not just `pr`: it commits locally
    like `local` does, but it does not carry the rest of the strictly-local
    rails (read-only `gh`, no external call of any kind), so accepting it
    would silently relax them."""
    return bool(proj["local_only_default"]) and delivery != "local"


def _prereq_names(fm):
    """Declared prerequisite task basenames, `.md` suffix stripped if present.
    Accepts YAML flow style `[a, b]` and the legacy space-separated `a b`."""
    names = config.split_values(fm.get("prerequisites", ""))
    return [n[:-3] if n.endswith(".md") else n for n in names]


ARCHIVE_DIR = "archive"


def task_path(root, name):
    """The file of task `name`: tasks/<name>.md, else tasks/archive/<name>.md
    when only the archived copy exists. A live file always wins, so a task
    copied back out of the archive to be reopened is the one read. When
    neither exists this is the live path, for callers to report as missing."""
    live = Path(root) / "tasks" / f"{name}.md"
    archived = Path(root) / "tasks" / ARCHIVE_DIR / f"{name}.md"
    return archived if not live.exists() and archived.exists() else live


def _is_done(root, name):
    """Whether task `name` is finished. A file in tasks/archive/ is done by
    definition, whatever its frontmatter says: only `archive_done` puts it
    there. A task that exists nowhere is not done (fail closed)."""
    path = task_path(root, name)
    if path.parent.name == ARCHIVE_DIR:
        return True
    return path.exists() and _frontmatter(path).get("status") == "done"


def _unmet_prerequisites(root, fm):
    """Names of this task's declared prerequisites that are not done.
    Resolution: basename -> tasks/<name>.md, else tasks/archive/<name>.md
    (see `task_path`). A prerequisite file that does not exist counts as unmet
    (fail closed), it never raises.
    Only the prerequisite's own status is read here, not its prerequisites in
    turn: no recursion happens, so a prerequisite cycle cannot loop."""
    return [name for name in _prereq_names(fm) if not _is_done(root, name)]


def _own_priority(fm):
    """The priority rank declared in the file, defaulting to `low`."""
    return PRIORITY_ORDER.get(fm.get("priority", "low"), PRIORITY_ORDER["low"])


def _all_frontmatter(root):
    """Map task basename -> frontmatter, for every task file."""
    return {p.stem: _frontmatter(p)
            for p in (Path(root) / "tasks").glob("*.md")
            if p.name != "TEMPLATE.md"}


def _effective_priorities(root):
    """Map task basename -> effective priority rank, lower being more urgent.

    A blocker inherits the best priority of everything still waiting on it,
    transitively: if `low` task A is a prerequisite of `high` task B, A is
    scheduled as `high`, because finishing A is the only way B ever runs.

    Only unfinished dependents propagate. A `done` task is not waiting on
    anything, so its priority must not keep pinning its old prerequisites at
    the front of the queue forever.

    Propagation is bounded relaxation rather than recursion: ranks only ever
    decrease and are bounded below by 0, so the loop terminates even when the
    prerequisite graph contains a cycle. `_unmet_prerequisites` deliberately
    does not recurse for the same reason; this function is the one place that
    walks the graph, and it is written not to trust it.
    """
    fms = _all_frontmatter(root)
    eff = {name: _own_priority(fm) for name, fm in fms.items()}
    # A dependent only pulls its blockers forward while it is still pending.
    pending = [n for n, fm in fms.items() if fm.get("status") != "done"]

    for _ in range(len(fms) + 1):
        changed = False
        for name in pending:
            rank = eff[name]
            for prereq in _prereq_names(fms[name]):
                if prereq in eff and rank < eff[prereq]:
                    eff[prereq] = rank
                    changed = True
        if not changed:
            break
    return eff


MARGIN_NIGHTS = 1


def _declared_due(fm):
    """(ok, date or None): the task's `due:` deadline. `ok` is False when the
    key is present but not an ISO date, which makes the task unschedulable
    and reported, like any unreadable value the engine interprets."""
    raw = fm.get("due")
    if raw is None:
        return True, None
    try:
        return True, date.fromisoformat(raw.strip("'\""))
    except ValueError:
        return False, None


def _nights(name, est_runs):
    """Nights one task needs: ceil(estimated runs), at least one."""
    return max(1, math.ceil((est_runs or {}).get(name, 1)))


def _deadlines(fms, est_runs=None):
    """Map task basename -> {"due", "latest_start"} for every unfinished task
    that is dated or that a dated task transitively waits on.

    `due` is the effective deadline: the earliest `due:` of the task and of
    every unfinished task depending on it, the way `_effective_priorities`
    propagates priority. `latest_start` is the last day the task can start
    and still let its whole chain (itself, then each dependent down to the
    dated one, one after the other) finish by that dependent's deadline; it
    is the minimum over every chain the task belongs to.

    Walked backwards from each dated task, never revisiting a task already on
    the current path: a prerequisite cycle is cut where it closes instead of
    pushing `latest_start` one night earlier on every lap."""
    pending = {n: fm for n, fm in fms.items() if fm.get("status") != "done"}
    out = {}

    def visit(name, due, start, path):
        cur = out.setdefault(name, {"due": due, "latest_start": start})
        cur["due"] = min(cur["due"], due)
        cur["latest_start"] = min(cur["latest_start"], start)
        for prereq in _prereq_names(pending[name]):
            if prereq in pending and prereq not in path:
                visit(prereq, due, date.fromordinal(
                    start.toordinal() - _nights(prereq, est_runs)),
                    path | {prereq})

    for name, fm in pending.items():
        _, due = _declared_due(fm)
        if due is not None:
            visit(name, due, date.fromordinal(
                due.toordinal() - _nights(name, est_runs)), {name})
    return out


def _due_soon(entry, today):
    """True when fewer than MARGIN_NIGHTS nights of slack are left before the
    task's chain must start: from then on it jumps the standard queue."""
    return (entry["latest_start"] - today).days < MARGIN_NIGHTS


def deadline_alerts(root, today, est_runs=None):
    """Deadlines the owner must hear about: list of (task filename, kind,
    due, act_by), kind being:

    - `overdue`: the task's own `due:` is in the past and it is not done;
    - `at_risk`: its chain can no longer fit before the (effective) deadline
      even at the front of the queue, or the task is `blocked` on the owner,
      so the chain waits on him. `act_by` is the last day the chain can
      start: the date by which the owner must act.

    Account-independent, printed once by gate.py status for the digest."""
    fms = _all_frontmatter(root)
    out = []
    for name, entry in sorted(_deadlines(fms, est_runs).items()):
        fm = fms[name]
        _, own = _declared_due(fm)
        if own is not None and own < today:
            out.append((f"{name}.md", "overdue", own, None))
            continue
        slack = (entry["latest_start"] - today).days
        if slack < 0 or fm.get("status") == "blocked":
            out.append((f"{name}.md", "at_risk", entry["due"],
                        entry["latest_start"]))
    return out


DUTY_PERIODS = ("nightly", "weekly")


def _sched_class(fm):
    """Scheduling class: "duty", "filler", or None for ordinary queued work.

    Any non-empty `duty:` value makes a task a duty, even an unsupported
    period: a typo must not silently demote the task into the priority queue,
    where its "mandatory" contract would no longer hold. Such a duty is never
    scheduled and is reported by `duties()`."""
    if str(fm.get("duty", "")).strip():
        return "duty"
    if str(fm.get("filler", "")).strip().lower() == "true":
        return "filler"
    return None


CLASS_EXPEDITE, CLASS_DUE_SOON, CLASS_STANDARD = 0, 1, 2


def _ordered(root, projects, account, sched_class=None, today=None,
             est_runs=None):
    """Eligible tasks for `account` in launch order.

    `sched_class` selects which scheduling class to return: None for the
    ordinary priority queue, "duty" or "filler" for the recurring classes.
    Every other eligibility rule is shared, so the classes cannot drift apart
    from the queue on prerequisites or account routing.

    `today` (a date, default the real one) decides which deadlines are due
    soon, and `est_runs` ({task basename: runs}) how many nights each task
    needs; see the module docstring for the order itself."""
    today = today or date.today()
    effective = _effective_priorities(root)
    dated = _deadlines(_all_frontmatter(root), est_runs)
    found = []
    for p in sorted((Path(root) / "tasks").glob("*.md")):
        if p.name == "TEMPLATE.md":
            continue
        fm = _frontmatter(p)
        if fm.get("status") != "ready":
            continue
        proj = projects.get(fm.get("project", "default"))
        if proj is None or proj["account"] != account:
            continue
        model = _declared_model(fm)
        if model is None:
            continue  # unknown model name, reported by misconfigured()
        if _unmet_prerequisites(root, fm):
            continue  # a hard prerequisite is not done yet
        if _sched_class(fm) != sched_class:
            continue  # a different scheduling class handles this one
        delivery = _declared_delivery(fm)
        if delivery is None:
            continue  # no delivery contract, reported by misconfigured()
        if _breaches_local_floor(delivery, proj):
            continue  # contradicts the project's local-only floor, same
        if "local_only" in fm:
            continue  # superseded key, also reported by misconfigured()
        if not _declared_due(fm)[0]:
            continue  # unreadable deadline, reported by misconfigured()
        if _declared_workdir(fm) is None:
            continue  # unknown workdir, reported by misconfigured()
        entry = dated.get(p.stem)
        if proj["expedite"]:
            cls, deadline = CLASS_EXPEDITE, 0
        elif entry is not None and _due_soon(entry, today):
            cls, deadline = CLASS_DUE_SOON, entry["due"].toordinal()
        else:
            cls, deadline = CLASS_STANDARD, 0
        key = (cls, deadline,
               effective.get(p.stem, _own_priority(fm)),
               proj["rank"],
               fm.get("created", "9999"), p.name)
        found.append((key, {"path": str(p), "model": model,
                            "effort": fm.get("effort", "low"),
                            "project": proj["name"],
                            "delivery": delivery,
                            "local_only": delivery == "local",
                            "parallel": fm.get("parallel") == "true",
                            "sched": _sched_class(fm),
                            "duty_period": str(fm.get("duty", "")).strip() or None}))
    return [t for _, t in sorted(found, key=lambda x: x[0])]


def task_name(ref):
    """Task basename from what an owner types: `a`, `a.md` or a path to it."""
    name = Path(ref).name
    return name[:-3] if name.endswith(".md") else name


def resolve(root, projects, account, ref):
    """One task named by the owner, for a manual launch: (task, unmet, reason).

    `task` is the same assignment `_ordered` builds, whatever the task's
    scheduling class, when the task is launchable right now. Otherwise it is
    None and either `unmet` lists prerequisites that are not done yet (the
    caller may wait for them) or `reason` says why it can never launch as it
    stands. Naming a task bypasses the ORDER, never the eligibility rules: a
    task the gatekeeper would refuse is refused here too, and said so."""
    name = task_name(ref)
    path = task_path(root, name)
    if not path.is_file():
        return None, [], "no such task"
    fm = _frontmatter(path)
    if fm.get("status") != "ready":
        return None, [], f"status={fm.get('status', '<missing>')}"
    proj = projects.get(fm.get("project", "default"))
    if proj is None:
        return None, [], f"project={fm.get('project', 'default')} not configured"
    if proj["account"] != account:
        return None, [], f"routes to account {proj['account']}"
    problems = [problem for n, problem in misconfigured(root, projects)
                if n == path.name]
    if problems:
        return None, [], "misconfigured: " + ", ".join(problems)
    unmet = _unmet_prerequisites(root, fm)
    if unmet:
        return None, unmet, None
    for cls in (None, "duty", "filler"):
        for t in _ordered(root, projects, account, sched_class=cls):
            if Path(t["path"]).stem == name:
                return t, [], None
    return None, [], "not eligible"


def duties_due(root, projects, account, done, period_keys):
    """Mandatory recurring tasks whose period has not been served yet.

    `done` maps task path -> the period key last recorded for it, and
    `period_keys` maps a duty period ("nightly", "weekly") to the key
    identifying the current one. A duty is due when the two differ, so a
    nightly duty runs once per night however many ticks that night has.
    Returned outside the priority ordering: duties are never starved.

    The clock stays with the caller - this module reads files, not time."""
    out = []
    for t in _ordered(root, projects, account, sched_class="duty"):
        key = period_keys.get(t["duty_period"])
        if key is None:
            continue  # unknown or out-of-window period, reported by duties()
        if done.get(t["path"]) != key:
            t["period_key"] = key
            out.append(t)
    return out


def duties(root, projects, account):
    """All ready duty tasks routed to `account`: (path, period, supported).
    Observability for gate.py status."""
    return [(t["path"], t["duty_period"], t["duty_period"] in DUTY_PERIODS)
            for t in _ordered(root, projects, account, sched_class="duty")]


def fillers(root, projects, account):
    """Opportunistic recurring tasks, in queue order. Callers must only launch
    these once the ordinary queue is served and budget is left over."""
    return _ordered(root, projects, account, sched_class="filler")


def blocked(root, projects, account):
    """Ready tasks routed to `account` whose prerequisites are unmet: list of
    (task filename, [unmet prerequisite names]). Pure observability for
    gate.py status."""
    out = []
    for p in sorted((Path(root) / "tasks").glob("*.md")):
        if p.name == "TEMPLATE.md":
            continue
        fm = _frontmatter(p)
        if fm.get("status") != "ready":
            continue
        proj = projects.get(fm.get("project", "default"))
        if proj is None or proj["account"] != account:
            continue
        unmet = _unmet_prerequisites(root, fm)
        if unmet:
            out.append((p.name, unmet))
    return out


def orphaned(root, projects):
    """Ready tasks that route to no project at all: list of (task filename,
    the unresolvable project name). A task naming a project the config does not
    declare is silently unschedulable - it matches no account, so no account's
    `blocked` report ever mentions it. This is the only place it surfaces.
    Account-independent by construction, so gate.py prints it once."""
    out = []
    for p in sorted((Path(root) / "tasks").glob("*.md")):
        if p.name == "TEMPLATE.md":
            continue
        fm = _frontmatter(p)
        if fm.get("status") != "ready":
            continue
        name = fm.get("project", "default")
        if name not in projects:
            out.append((p.name, name))
    return out


STUCK_NOTE = (
    "- {date}: auto-repaired by the gatekeeper. Left `in-progress` with no "
    "live session behind it for {hours:.0f}h, which makes a task "
    "unschedulable forever (only `ready` is ever picked). Status set back to "
    "`ready`. The notes above are the last thing that session recorded - "
    "resume from them, do not restart from scratch.\n")


def set_status(path, status, note):
    """Rewrite one task's frontmatter status and log why in Notes.

    The status line is replaced only inside the frontmatter block, so a task
    whose prose happens to contain a `status:` line is not corrupted. A task
    with no `## Notes` section gets one: the note is the only trace of the
    change the human ever sees in the task itself. `note` is one complete
    Markdown line, newline included.
    """
    text = path.read_text(errors="replace")
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return False
    fm = re.sub(r"^status:.*$", f"status: {status}", m.group(1),
                count=1, flags=re.M)
    text = text[:m.start(1)] + fm + text[m.end(1):]
    if "\n## Notes" in text:
        head, sep, tail = text.rpartition("\n## Notes")
        body = tail.split("\n", 1)
        rest = body[1] if len(body) > 1 else ""
        text = f"{head}{sep}{body[0]}\n{rest.rstrip()}\n{note}"
    else:
        text = f"{text.rstrip()}\n\n## Notes\n\n{note}"
    path.write_text(text)
    return True


def _set_ready(path, hours, date):
    """Repair one stuck task: back to `ready`, with STUCK_NOTE explaining."""
    return set_status(path, "ready", STUCK_NOTE.format(date=date, hours=hours))


CLAIM_NOTE = (
    "- {date}: claimed by the gatekeeper at launch ({model}, slice {slice_min} "
    "min); the session records its own progress below.\n")


def claim(path, date, model, slice_min):
    """Mark one queue task `in-progress` at LAUNCH, before its session exists.

    The session used to set `in-progress` itself, several minutes after
    launch. Between the two, the task was still `ready` to the scheduler: on
    2026-09-12 the tick after a launch picked the same task again and a second
    session worked it in the same worktree, for 4.7M tokens. Claiming here
    closes that window; the tick that prints RUN is the one that owns the file.

    A claim whose session never gets to the task (crash before step 4, or a
    launch that failed) is not a leak: `repair_stuck` resets any `in-progress`
    older than the lock TTL back to `ready`, with a note.
    """
    return set_status(Path(path), "in-progress",
                      CLAIM_NOTE.format(date=date, model=model,
                                        slice_min=slice_min))


def repair_stuck(root, now, ttl_s, date):
    """Reset tasks stuck `in-progress` back to `ready`: list of (filename, hours).

    A session is killed hard when its slice runs out, and the machine can go
    down mid-run. Either way the task keeps the `in-progress` it set on itself
    and becomes permanently invisible to the scheduler, silently - three tasks
    on this backlog sat that way for a month. The owner ruled once that he
    would rather handle these by hand and revisit if it happened three times;
    it has now happened five, so the engine repairs them.

    Liveness is inferred from the file's own mtime rather than from the RUNNING
    locks, because the locks do not record which task they hold. A session
    cannot outlive its slice (hard kill at slice + 10 minutes, an hour at the
    configured slice), so `ttl_s` at the lock TTL leaves no window in which a
    working session's task could be reset under it.
    """
    out = []
    for p in sorted((Path(root) / "tasks").glob("*.md")):
        if p.name == "TEMPLATE.md":
            continue
        if _frontmatter(p).get("status") != "in-progress":
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < ttl_s:
            continue
        if _set_ready(p, age / 3600.0, date):
            out.append((p.name, age / 3600.0))
    return out


def misconfigured(root, projects):
    """Ready tasks the engine refuses to schedule because a frontmatter value
    it interprets is unreadable: list of (task filename, `key=problem`).

    Plays the same role for values that `orphaned` plays for projects. Both
    conditions make a task invisible rather than merely unscheduled: it enters
    no account's queue, so no `blocked` report ever mentions it, and this is
    the only place it surfaces. Reported here:

    - `model:` naming something outside MODELS (sonnet, opus, fable),
      typically a CLI model id.
    - `delivery:` absent, or naming something outside DELIVERY_VALUES.
    - `delivery:` breaching the project's local-only floor.
    - a leftover `local_only:` key, which `delivery:` replaced.
    - `due:` that is not an ISO date.
    - a file in tasks/archive/ whose status is not `done`: archived means
      done, so the edit would otherwise be silently ignored.
    - `workdir:` other than `main`.

    One task can be reported for several of these; each line names its key, so
    fixing the file needs no guessing. Account-independent by construction, so
    gate.py prints it once."""
    out = []
    for p in sorted((Path(root) / "tasks").glob("*.md")):
        if p.name == "TEMPLATE.md":
            continue
        fm = _frontmatter(p)
        if fm.get("status") != "ready":
            continue
        if _declared_model(fm) is None:
            out.append((p.name, f"model={fm['model']}"))
        delivery = _declared_delivery(fm)
        proj = projects.get(fm.get("project", "default"))
        if delivery is None:
            out.append((p.name, f"delivery={fm.get('delivery', '<missing>')}"))
        elif proj is not None and _breaches_local_floor(delivery, proj):
            out.append((p.name, f"delivery={delivery} in local-only project "
                                f"{proj['name']}"))
        if "local_only" in fm:
            out.append((p.name, "local_only= (replaced by delivery:, "
                                "remove the key)"))
        if not _declared_due(fm)[0]:
            out.append((p.name, f"due={fm['due']} (expected YYYY-MM-DD)"))
        if _declared_workdir(fm) is None:
            out.append((p.name, f"workdir={fm['workdir']} (only `main`, or "
                                "no key for a worktree)"))
    # An archived file is done by definition, so editing its status reopens
    # nothing: surface the edit instead of silently ignoring it.
    for p in sorted((Path(root) / "tasks" / ARCHIVE_DIR).glob("*.md")):
        status = _frontmatter(p).get("status")
        if status != "done":
            out.append((f"{ARCHIVE_DIR}/{p.name}",
                        f"status={status} in {ARCHIVE_DIR}/ (move it back to "
                        "tasks/ to reopen it)"))
    return out


def pick(root, projects, account, today=None, est_runs=None):
    """Best task for the account: {"path", "model", "effort", "project",
    "delivery", "local_only", "parallel"} or None."""
    picked = pick_multi(root, projects, account, 1, today, est_runs)
    return picked[0] if picked else None


class _ModelSlots:
    """How many more sessions each model family may get this tick.

    `{family: remaining}`; a family absent from the dict is unlimited, one at
    0 is skipped. `take` answers whether a task can still be launched and, if
    so, consumes one of its family's slots, so the selection below filters
    WHILE it slices rather than after: the caller used to cut the queue to the
    slot count first and drop the unlaunchable models second, which on
    2026-09-12 turned [opus, fable, fable, sonnet] with four free slots into
    two launches, on every tick of the night."""

    def __init__(self, model_slots):
        self.left = dict(model_slots or {})

    def take(self, task):
        n = self.left.get(task["model"])
        if n is None:
            return True
        if n <= 0:
            return False
        self.left[task["model"]] = n - 1
        return True


def _take(candidates, count, slots):
    """The first `count` of `candidates` that `slots` lets through, consuming
    their model slots as they are picked."""
    out = []
    for t in candidates:
        if len(out) >= count:
            break
        if slots.take(t):
            out.append(t)
    return out


def launchable(candidates, count, model_slots):
    """The first `count` of `candidates` the model slots let through, in order.
    `_take` for callers that build their own candidate list (manual.py)."""
    return _take(candidates, count, _ModelSlots(model_slots))


def _pad_parallel(out, queue, count, slots):
    """Fill leftover slots with extra sessions on tasks that declare
    `parallel: true` (they shard via claims). Padding is the last resort: it
    only duplicates work already selected, so every other class goes first.
    A shard is one more session of its model, so it obeys the model slots too."""
    par = [t for t in queue if t["parallel"]]
    while 0 < len(out) < count and par:
        # One round over the shards; a round that admits nothing means the
        # model slots are exhausted for every shard, so stop.
        admitted = False
        for t in par:
            if len(out) >= count:
                break
            if slots.take(t):
                out.append(dict(t))
                admitted = True
        if not admitted:
            break
    return out


def pick_multi(root, projects, account, count, today=None, est_runs=None):
    """Up to `count` session assignments from the ordinary priority queue:
    distinct tasks first, then parallel shards."""
    queue = _ordered(root, projects, account, today=today, est_runs=est_runs)
    slots = _ModelSlots(None)
    return _pad_parallel(queue[:count], queue, count, slots)


def launch_order(root, projects, account, count,
                 done=None, period_keys=None, model_slots=None,
                 today=None, est_runs=None, exclude=()):
    """Up to `count` session assignments for one tick, in launch order.

    The three scheduling classes are served in a fixed order that encodes their
    contract:

    1. duties - mandatory recurring work, taken off the top so the priority
       queue can never starve them;
    2. the ordinary priority queue (expedite, due soon, standard);
    3. fillers - opportunistic recurring work, reached only once the queue is
       exhausted (the caller additionally launches them only if budget is left);
    4. parallel shards of queue tasks, as padding.

    `model_slots` (`{family: remaining}`, see _ModelSlots) says which models
    the caller can still launch and how many times; the returned list already
    respects it, so it fills `count` whenever enough launchable work exists.
    A duty whose model has no slot is not starved by this - it cannot run
    anyway - and comes back the next tick, its period still unserved.

    `exclude` holds queue task basenames the caller will not launch this tick
    (stalls.ladder_blocked); the next ones in the queue take their slots.
    `today` and `est_runs` order the queue's deadlines, see `_ordered`.

    Each assignment carries `sched` ("duty", "filler" or None) so the caller can
    apply the budget rule that matches the class."""
    if count <= 0:
        return []
    slots = _ModelSlots(model_slots)
    out = _take(duties_due(root, projects, account, done or {}, period_keys or {}),
                count, slots)
    queue = [t for t in _ordered(root, projects, account, today=today, est_runs=est_runs)
             if Path(t["path"]).name not in exclude]
    out += _take(queue, count - len(out), slots)
    if len(out) < count:
        key = (period_keys or {}).get("filler")
        due = []
        for t in fillers(root, projects, account):
            if key is not None and (done or {}).get(t["path"]) == key:
                continue  # already served tonight
            t["period_key"] = key
            due.append(t)
        out += _take(due, count - len(out), slots)
    return _pad_parallel(out, queue, count, slots)


def archive_done(root, now, ttl_s):
    """Move every `done` task from tasks/ to tasks/archive/: list of filenames.

    This is the one archiving step, run by the gatekeeper on every tick, so a
    task set `done` by a session or by hand leaves the live backlog without
    anyone having to remember a `mv`. The scheduler and the sessions keep
    globbing tasks/*.md only; lookups by name go through `task_path`, which
    also resolves the archive.

    A file modified less than `ttl_s` ago is left alone, as in `repair_stuck`:
    the session that just set it `done` may still be writing its notes or
    committing it, and moving the file under it would make a later append
    recreate a frontmatter-less stub at the old path. At the lock TTL no
    working session can still hold the file.

    An archived copy of the same name is replaced: the live file is the one a
    reopened task was worked from, so it is the newer record."""
    archive = Path(root) / "tasks" / ARCHIVE_DIR
    moved = []
    for p in sorted((Path(root) / "tasks").glob("*.md")):
        if p.name == "TEMPLATE.md" or _frontmatter(p).get("status") != "done":
            continue
        try:
            if now - p.stat().st_mtime < ttl_s:
                continue
            archive.mkdir(exist_ok=True)
            p.replace(archive / p.name)
        except OSError:
            continue
        moved.append(p.name)
    return moved
