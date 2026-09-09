"""Select the next eligible ready tasks for one account from the backlog.

Selection is deterministic and happens in the gatekeeper (not in the session),
because the task's declared model must be known before launching the session.
Frontmatter keys honored: status, project, local_only, priority, created,
model (sonnet|opus|fable, default sonnet), effort (low|medium|high, default low),
prerequisites (space-separated task basenames, `.md` suffix optional).

Eligibility: `status: ready`, and the task's `project:` must exist in the
config's project registry AND belong to the account currently being scheduled.
A task without a `project:` key routes to the project named "default" when one
exists (the legacy single-account fallback synthesizes it). The tool imposes
no taxonomy beyond that: projects are whatever the config declares. A ready task
whose project does not resolve can never be scheduled; `orphaned` reports those
so a typo or a renamed project does not silently swallow work.

Ordering within an account: project `priority` (integer, lower preferred),
then task `priority` (high/medium/low), then oldest `created`, then filename.

`local_only` is per task; when absent it inherits the project's
`local_only_default`. It is carried through to the session prompt, which
enforces the strictly-local rails (nothing leaves the machine).

The declared `model:` is a FLOOR, never a ceiling: a task may be upgraded to a
stronger model by the gatekeeper (pre-reset burn-down), never downgraded. A task
whose floor exceeds what the current regime/budget allows is skipped.
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
  work. Good for open-ended chores that are nice to advance but never urgent.

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
import re
from pathlib import Path

from lib import config

PRIORITY_ORDER = {"high": 0, "medium": 1, "normal": 1, "low": 2}
MODEL_RANK = {"sonnet": 0, "opus": 1, "fable": 2}


def _frontmatter(path):
    text = path.read_text(errors="replace")
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip() and v.strip():
                fm[k.strip()] = v.strip()
    return fm


def _declared_model(fm):
    """The task's declared model floor, or None when it names one the engine
    does not know.

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
    return raw if raw in MODEL_RANK else None


def _prereq_names(fm):
    """Declared prerequisite task basenames, `.md` suffix stripped if present.
    Accepts YAML flow style `[a, b]` and the legacy space-separated `a b`."""
    names = config.split_values(fm.get("prerequisites", ""))
    return [n[:-3] if n.endswith(".md") else n for n in names]


def _unmet_prerequisites(root, fm):
    """Names of this task's declared prerequisites that are not `status: done`.
    Resolution: basename -> tasks/<name>.md. A prerequisite file that does not
    exist counts as unmet (fail closed), it never raises.
    Only the prerequisite's own status is read here, not its prerequisites in
    turn: no recursion happens, so a prerequisite cycle cannot loop."""
    unmet = []
    for name in _prereq_names(fm):
        path = Path(root) / "tasks" / f"{name}.md"
        if not path.exists() or _frontmatter(path).get("status") != "done":
            unmet.append(name)
    return unmet


def _own_priority(fm):
    """The priority rank declared in the file, defaulting to `low`."""
    return PRIORITY_ORDER.get(fm.get("priority", "low"), PRIORITY_ORDER["low"])


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
    fms = {}
    for p in (Path(root) / "tasks").glob("*.md"):
        if p.name != "TEMPLATE.md":
            fms[p.stem] = _frontmatter(p)

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


def _ordered(root, projects, account, max_model, sched_class=None):
    """Eligible tasks for `account` in launch order.

    `sched_class` selects which scheduling class to return: None for the
    ordinary priority queue, "duty" or "filler" for the recurring classes.
    Every other eligibility rule is shared, so the classes cannot drift apart
    from the queue on prerequisites, model floors or account routing."""
    ceiling = MODEL_RANK.get(max_model, 0)
    effective = _effective_priorities(root)
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
        if MODEL_RANK[model] > ceiling:
            continue  # floor above what this tick may launch
        if _unmet_prerequisites(root, fm):
            continue  # a hard prerequisite is not done yet
        if _sched_class(fm) != sched_class:
            continue  # a different scheduling class handles this one
        if "local_only" in fm:
            local_only = fm["local_only"] == "true"
        else:
            local_only = proj["local_only_default"]
        key = (proj["priority"],
               effective.get(p.stem, _own_priority(fm)),
               fm.get("created", "9999"), p.name)
        found.append((key, {"path": str(p), "model": model,
                            "effort": fm.get("effort", "low"),
                            "project": proj["name"],
                            "local_only": local_only,
                            "parallel": fm.get("parallel") == "true",
                            "sched": _sched_class(fm),
                            "duty_period": str(fm.get("duty", "")).strip() or None}))
    return [t for _, t in sorted(found, key=lambda x: x[0])]


def duties_due(root, projects, account, max_model, done, period_keys):
    """Mandatory recurring tasks whose period has not been served yet.

    `done` maps task path -> the period key last recorded for it, and
    `period_keys` maps a duty period ("nightly", "weekly") to the key
    identifying the current one. A duty is due when the two differ, so a
    nightly duty runs once per night however many ticks that night has.
    Returned outside the priority ordering: duties are never starved.

    The clock stays with the caller - this module reads files, not time."""
    out = []
    for t in _ordered(root, projects, account, max_model, sched_class="duty"):
        key = period_keys.get(t["duty_period"])
        if key is None:
            continue  # unknown or out-of-window period, reported by duties()
        if done.get(t["path"]) != key:
            t["period_key"] = key
            out.append(t)
    return out


def duties(root, projects, account):
    """All ready duty tasks routed to `account`: (path, period, supported).
    Model ceiling ignored - this is observability for gate.py status."""
    return [(t["path"], t["duty_period"], t["duty_period"] in DUTY_PERIODS)
            for t in _ordered(root, projects, account, "fable", sched_class="duty")]


def fillers(root, projects, account, max_model):
    """Opportunistic recurring tasks, in queue order. Callers must only launch
    these once the ordinary queue is served and budget is left over."""
    return _ordered(root, projects, account, max_model, sched_class="filler")


def blocked(root, projects, account):
    """Ready tasks routed to `account` whose prerequisites are unmet: list of
    (task filename, [unmet prerequisite names]). Model ceiling is irrelevant
    here, this is pure observability for gate.py status."""
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


def _set_ready(path, hours, date):
    """Rewrite one task's frontmatter status to `ready` and log why in Notes.

    The status line is replaced only inside the frontmatter block, so a task
    whose prose happens to contain a `status:` line is not corrupted. A task
    with no `## Notes` section gets one: the note is the only trace of the
    repair the human ever sees in the task itself.
    """
    text = path.read_text(errors="replace")
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return False
    fm = re.sub(r"^status:.*$", "status: ready", m.group(1),
                count=1, flags=re.M)
    text = text[:m.start(1)] + fm + text[m.end(1):]
    note = STUCK_NOTE.format(date=date, hours=hours)
    if "\n## Notes" in text:
        head, sep, tail = text.rpartition("\n## Notes")
        body = tail.split("\n", 1)
        rest = body[1] if len(body) > 1 else ""
        text = f"{head}{sep}{body[0]}\n{rest.rstrip()}\n{note}"
    else:
        text = f"{text.rstrip()}\n\n## Notes\n\n{note}"
    path.write_text(text)
    return True


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


def misconfigured(root):
    """Ready tasks the engine refuses to schedule because their `model:` names
    a model it does not know: list of (task filename, the unreadable value).

    Plays the same role for models that `orphaned` plays for projects. Both
    conditions make a task invisible rather than merely unscheduled: it enters
    no account's queue, so no `blocked` report ever mentions it, and this is
    the only place it surfaces. Model names the engine accepts: the keys of
    MODEL_RANK (sonnet, opus, fable), never CLI model ids.
    Account-independent by construction, so gate.py prints it once."""
    out = []
    for p in sorted((Path(root) / "tasks").glob("*.md")):
        if p.name == "TEMPLATE.md":
            continue
        fm = _frontmatter(p)
        if fm.get("status") != "ready":
            continue
        if _declared_model(fm) is None:
            out.append((p.name, fm["model"]))
    return out


def pick(root, projects, account, max_model):
    """Best task for the account: {"path", "model", "effort", "project",
    "local_only", "parallel"} or None."""
    picked = pick_multi(root, projects, account, max_model, 1)
    return picked[0] if picked else None


def _pad_parallel(out, queue, count):
    """Fill leftover slots with extra sessions on tasks that declare
    `parallel: true` (they shard via claims). Padding is the last resort: it
    only duplicates work already selected, so every other class goes first."""
    par = [t for t in queue if t["parallel"]]
    i = 0
    while 0 < len(out) < count and par:
        out.append(dict(par[i % len(par)]))
        i += 1
    return out


def pick_multi(root, projects, account, max_model, count):
    """Up to `count` session assignments from the ordinary priority queue:
    distinct tasks first, then parallel shards."""
    queue = _ordered(root, projects, account, max_model)
    return _pad_parallel(queue[:count], queue, count)


def launch_order(root, projects, account, max_model, count,
                 done=None, period_keys=None):
    """Up to `count` session assignments for one tick, in launch order.

    The three scheduling classes are served in a fixed order that encodes their
    contract:

    1. duties - mandatory recurring work, taken off the top so the priority
       queue can never starve them;
    2. the ordinary priority queue;
    3. fillers - opportunistic recurring work, reached only once the queue is
       exhausted (the caller additionally launches them only if budget is left);
    4. parallel shards of queue tasks, as padding.

    Each assignment carries `sched` ("duty", "filler" or None) so the caller can
    apply the budget rule that matches the class."""
    if count <= 0:
        return []
    out = duties_due(root, projects, account, max_model,
                     done or {}, period_keys or {})[:count]
    queue = _ordered(root, projects, account, max_model)
    out += queue[:count - len(out)]
    if len(out) < count:
        out += fillers(root, projects, account, max_model)[:count - len(out)]
    return _pad_parallel(out, queue, count)
