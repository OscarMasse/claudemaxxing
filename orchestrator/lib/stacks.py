"""Stacks: linear PR-on-PR dependencies shown, and briefed, as GitHub stacks.

There is no stack key. A task whose single open-PR prerequisite it can stack
on (lib/tasks.py `stack_base`) is the next layer of that prerequisite's stack,
so a stack is a chain of tasks, named after its bottom layer.

The digest asks `gate.py stacks` for the view: layers in stack order, each
with its task status and PR state, a "ready to merge" verdict once every
layer is done and its PR is open with green checks, then the tasks that wait
for a merge instead of stacking, with the reason. GitHub does the merging,
never us.

CLI (run.sh):
  python3 lib/stacks.py directive <backlog_root> <task_file>
prints the prompt paragraph for a stacked task, or nothing.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import tasks  # noqa: E402


def _live_pr_tasks(root):
    return {name: fm for name, fm in tasks._all_frontmatter(root).items()
            if tasks._declared_delivery(fm) == "pr"}


def collect(root):
    """`(stacks, waits)`. `stacks` maps a stack name (its bottom task) to its
    layer dicts, bottom first; `waits` lists `(task, prerequisites, reason)`
    for the live tasks whose open-PR prerequisites must be merged first.
    Archived tasks are not live work: a stack whose bottom layer is archived
    is named after its lowest live layer."""
    parent, waits = {}, []
    for name, fm in sorted(_live_pr_tasks(root).items()):
        base, reason = tasks.stack_base(root, name)
        if base:
            parent[name] = base["task"]
        elif reason and fm.get("status") != "done":
            waits.append((name, tasks._open_pr_prereqs(root, fm), reason))
    children = {p: c for c, p in parent.items()}
    live = tasks._all_frontmatter(root)
    out = {}
    for bottom in sorted(set(parent.values()) | set(parent)):
        if bottom in parent and parent[bottom] in live:
            continue  # not the lowest live layer
        if bottom not in children:
            continue  # a single task is not a stack
        chain, n = [], bottom
        while n is not None:
            urls = tasks._pr_urls(tasks.task_path(root, n))
            fm = tasks._frontmatter(tasks.task_path(root, n))
            chain.append({"task": n, "status": fm.get("status", "?"),
                          "pr": urls[-1] if urls else None})
            n = children.get(n)
        out[bottom] = chain
    return out, waits


def pr_state(url):
    """(state, checks) of a PR via gh; checks is "green", "red", "pending" or
    "none". Any failure gives ("unknown", "none")."""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state,statusCheckRollup"],
            capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        data = {}
    if not data:
        return "unknown", "none"
    results = [c.get("conclusion") or c.get("state") or "" for c in
               data.get("statusCheckRollup") or []]
    results = [x.upper() for x in results]
    if not results:
        checks = "none"
    elif any(x in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT",
                   "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE") for x in results):
        checks = "red"
    elif all(x in ("SUCCESS", "NEUTRAL", "SKIPPED") for x in results):
        checks = "green"
    else:
        checks = "pending"
    return data.get("state", "unknown"), checks


def render(collected, fetch=pr_state):
    """Markdown lines: one block per stack, then one line per waiting task."""
    stacks, waits = collected
    lines = []
    for stack, layers in sorted(stacks.items()):
        ready = True
        rows = []
        for i, layer in enumerate(layers, 1):
            if layer["pr"]:
                state, checks = fetch(layer["pr"])
            else:
                state, checks = "no PR", "none"
            if state == "MERGED":
                pass  # a merged bottom layer is fine (partial merge)
            elif not (layer["status"] == "done" and state == "OPEN"
                      and checks == "green"):
                ready = False
            rows.append(f"  {i}. {layer['task']} - {layer['status']}, "
                        f"PR {state}, checks {checks}")
        verdict = "ready to merge" if ready else "not ready"
        lines.append(f"- stack `{stack}` ({len(layers)} layers): {verdict}")
        lines.extend(rows)
    for task, prereqs, reason in waits:
        lines.append(f"- `{task}` waits for the merge of "
                     f"{', '.join(prereqs)} ({reason})")
    return lines


DIRECTIVE = (
    "This task is stacked: its prerequisite {task} is done but its PR {url} "
    "is not merged yet, so your worktree branch was created from that PR's "
    "branch `{head}`, not from the default branch. Build your change as the "
    "next layer of that GitHub stack. At the start of every slice run `git "
    "fetch origin {head}` and rebase your branch onto `origin/{head}`, so you "
    "build on the lower layer as it is now. To publish: push your branch by "
    "its explicit HTTPS URL as usual, open your PR with `gh pr create --base "
    "{head}` (never against the default branch, it would carry the lower "
    "layer's commits), then run `gh stack link {url} <your PR URL>` to add it "
    "on top of the stack. Never merge any layer.")


def directive(root, name):
    """The prompt paragraph for task `name` when it stacks, else ""."""
    base, _ = tasks.stack_base(root, name)
    return DIRECTIVE.format(**base) if base else ""


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "directive":
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    print(directive(sys.argv[2], Path(sys.argv[3]).stem))
