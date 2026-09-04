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
no taxonomy beyond that: projects are whatever the config declares.

Ordering within an account: project `priority` (integer, lower preferred),
then task `priority` (high/medium/low), then oldest `created`, then filename.

`local_only` is per task; when absent it inherits the project's
`local_only_default`. It is carried through to the session prompt, which
enforces the strictly-local rails (nothing leaves the machine).

The declared `model:` is a FLOOR, never a ceiling: a task may be upgraded to a
stronger model by the gatekeeper (pre-reset burn-down), never downgraded. A task
whose floor exceeds what the current regime/budget allows is skipped.

Prerequisite gating: a task with a `prerequisites:` key is not eligible until
every named prerequisite task is itself `status: done`. Only the prerequisite's
own status is read, never its own prerequisites, so this check never recurses
and cycles cannot cause a loop.
"""
import re
from pathlib import Path

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


def _prereq_names(fm):
    """Declared prerequisite task basenames, `.md` suffix stripped if present."""
    raw = fm.get("prerequisites", "")
    return [n[:-3] if n.endswith(".md") else n for n in raw.split()]


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


def _ordered(root, projects, account, max_model):
    """Eligible tasks for `account` in launch order."""
    ceiling = MODEL_RANK.get(max_model, 0)
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
        model = fm.get("model", "sonnet")
        if model not in MODEL_RANK:
            model = "sonnet"
        if MODEL_RANK[model] > ceiling:
            continue  # floor above what this tick may launch
        if _unmet_prerequisites(root, fm):
            continue  # a hard prerequisite is not done yet
        if "local_only" in fm:
            local_only = fm["local_only"] == "true"
        else:
            local_only = proj["local_only_default"]
        key = (proj["priority"],
               PRIORITY_ORDER.get(fm.get("priority", "low"), 2),
               fm.get("created", "9999"), p.name)
        found.append((key, {"path": str(p), "model": model,
                            "effort": fm.get("effort", "low"),
                            "project": proj["name"],
                            "local_only": local_only,
                            "parallel": fm.get("parallel") == "true"}))
    return [t for _, t in sorted(found, key=lambda x: x[0])]


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


def pick(root, projects, account, max_model):
    """Best task for the account: {"path", "model", "effort", "project",
    "local_only", "parallel"} or None."""
    picked = pick_multi(root, projects, account, max_model, 1)
    return picked[0] if picked else None


def pick_multi(root, projects, account, max_model, count):
    """Up to `count` session assignments: distinct tasks first, then extra
    sessions on tasks that declare `parallel: true` (they shard via claims)."""
    ordered = _ordered(root, projects, account, max_model)
    out = ordered[:count]
    par = [t for t in ordered if t["parallel"]]
    i = 0
    while 0 < len(out) < count and par:
        out.append(par[i % len(par)])
        i += 1
    return out
