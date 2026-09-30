"""Stack view: each GitHub stack (tasks sharing `stack:`) shown as one unit.

The digest asks `gate.py stacks` for it: layers in stack order, each with its
task status and PR state, and a "ready to merge" verdict once every layer is
done and its PR is open with green checks. GitHub does the merging, never us.
"""
import json
import subprocess

from lib import tasks


def collect(root):
    """Map stack name -> list of layer dicts in stack order (archived tasks
    are not live work, so they do not appear)."""
    fms = tasks._all_frontmatter(root)
    by_stack = {}
    for name, fm in fms.items():
        stack = tasks._declared_stack(fm)
        if stack:
            by_stack.setdefault(stack, []).append(name)
    out = {}
    for stack, names in by_stack.items():
        members = set(names)

        def depth(n, seen=(), members=members):
            deps = [d for d in (fms[n].get("prerequisites") or "")
                    .replace(",", " ").split()
                    if d in members and d not in seen]
            return 1 + max((depth(d, seen + (n,), members) for d in deps), default=0)

        layers = []
        for n in sorted(names, key=lambda n: (depth(n), n)):
            urls = tasks._pr_urls(tasks.task_path(root, n))
            layers.append({"task": n, "status": fms[n].get("status", "?"),
                           "pr": urls[-1] if urls else None})
        out[stack] = layers
    return out


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


def render(stacks, fetch=pr_state):
    """Markdown lines, one block per stack."""
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
    return lines
