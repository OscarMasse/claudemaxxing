"""Tear down docker compose stacks that sessions left running in agent worktrees.

Why this exists
---------------
A session that brings up a project's docker stack (dev servers, E2E services)
exits without `docker compose down` as often as not: it is killed at the end
of its slice, it forgets, or it keeps the stack up "for the next slice". On
2026-09-24 eighteen such containers were still up, the oldest for 13 days,
some for tasks already `done`. One of them held host port 5432, and that
night's session could not start its own E2E stack and reported the task
unverified because of it. Leftovers are not a cost problem, they are a
correctness problem: they turn every later session's environment into
whatever the previous ones abandoned.

What it reaps
-------------
Only compose projects whose working directory is an agent worktree: a path
with a `.agent-worktrees` component, inside a configured project dir. That is
where the session protocol puts agent work, so the owner's own checkouts and
anything else on the machine (other repos, work stacks) are never touched.
`down` without `-v`: containers and networks go, named volumes stay, so no
data is lost and a later slice that needs the stack just brings it up again.

When it reaps is decided by the caller (gate.py), not here.
"""
import os
import subprocess
from pathlib import Path

WORKTREES_DIRNAME = ".agent-worktrees"
_FORMAT = ('{{.Label "com.docker.compose.project"}}\t'
           '{{.Label "com.docker.compose.project.working_dir"}}')


def _docker():
    """Docker CLI to call, or None when disabled. ORCH_DOCKER_BIN overrides
    the binary; set to an empty string it turns the janitor off (tests)."""
    return os.environ.get("ORCH_DOCKER_BIN", "docker") or None


def reapable(rows, project_dirs):
    """Compose project names, among `(project, working_dir)` rows, that live in
    an agent worktree under one of `project_dirs`. Sorted, deduplicated."""
    roots = [Path(d).resolve() for d in project_dirs]
    out = set()
    for project, working_dir in rows:
        if not project or not working_dir:
            continue
        wd = Path(working_dir).resolve()
        if WORKTREES_DIRNAME not in wd.parts:
            continue
        if any(wd.is_relative_to(r) for r in roots):
            out.add(project)
    return sorted(out)


def list_stacks():
    """`(project, working_dir)` of every compose-managed container, running or
    stopped. Empty when docker is disabled, missing, or its daemon is down:
    no docker means nothing to reap, not an error."""
    docker = _docker()
    if not docker:
        return []
    try:
        r = subprocess.run([docker, "ps", "-a", "--format", _FORMAT],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    rows = []
    for line in r.stdout.splitlines():
        project, _, working_dir = line.partition("\t")
        rows.append((project.strip(), working_dir.strip()))
    return rows


def down(project):
    """`docker compose -p <project> down --remove-orphans`. True on success."""
    docker = _docker()
    if not docker:
        return False
    try:
        r = subprocess.run([docker, "compose", "-p", project, "down",
                            "--remove-orphans"],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def sweep(project_dirs):
    """Tear down every agent-worktree stack: list of (project, ok)."""
    return [(name, down(name))
            for name in reapable(list_stacks(), project_dirs)]
