"""Give each background session its own git worktree instead of a main checkout.

Why this exists
---------------
On 2026-09-25 a session ran `git checkout -b <branch> origin/main` inside the
rankr main checkout while another session had uncommitted work there. The
switch carried the other session's dirty files into the new branch, and a
`git stash` or `git reset --hard` at the wrong moment would have destroyed
them. Leaving the choice of a worktree to each session's judgement is what
failed, so the launcher makes it instead: run.sh asks this module for the
directories a session may write to, and every repo among the task's project
dirs comes back as a worktree dedicated to that task.

The layout
----------
`<repo>/.agent-worktrees/<task-slug>` on branch `agent/<task-slug>`, created
from `origin/main` (fetched first, best effort). A later slice of the same
task reuses the worktree, or re-attaches the branch when only the branch is
left. The deterministic names are the record: nothing else has to remember
which worktree belongs to which task.

What is not a repo dir
----------------------
Only a dir that is itself the top level of a git repo gets a worktree. The
backlog root is written in place by design (task files, digests), and a
parent dir holding several repos (`~/side-projects`) names no single repo, so
both are passed through unchanged. A task that must work in the main
checkout itself (rankr seeds in its gitignored `scripts/`) declares
`workdir: main`, validated in lib/tasks.py.

Cleanup
-------
`reap` (called by gate.py next to the compose janitor) removes a task's
worktree once its branch is merged into origin/main or its PR is merged or
closed, and only when it has no uncommitted or untracked changes; a dirty one
is returned for the log, never deleted. Only worktrees this module creates
are candidates: `.agent-worktrees/<slug>` on `agent/<slug>`.

CLI (run.sh):
  python3 lib/workspace.py dirs <task_file> <backlog_root> <dir>...
prints one session dir per line, in order. Exit 1, with the reason on
stderr, when a worktree cannot be prepared: the launcher then does not start
the session rather than start it in a main checkout.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import tasks  # noqa: E402

WORKTREES_DIRNAME = ".agent-worktrees"  # also what lib/janitor.py matches
BRANCH_PREFIX = "agent/"
BASES = ("origin/main", "main")


class WorkspaceError(RuntimeError):
    pass


def _git(repo, *args, timeout=60):
    # Headless: a fetch that needs credentials it lacks must fail, not wait
    # for a terminal prompt that never comes.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", str(repo), *args], env=env,
                          capture_output=True, text=True, timeout=timeout)


def repo_toplevel(d):
    """True when `d` is the top level of a git repo (not a subdir, not a
    parent of repos)."""
    try:
        r = _git(d, "rev-parse", "--show-toplevel", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (r.returncode == 0
            and Path(r.stdout.strip()).resolve() == Path(d).resolve())


def _has_ref(repo, ref):
    return _git(repo, "rev-parse", "--verify", "--quiet",
                f"{ref}^{{commit}}").returncode == 0


def _exclude_worktrees(repo):
    """Keep `.agent-worktrees/` out of the main checkout's `git status`,
    through the repo-local exclude file (never a tracked .gitignore)."""
    r = _git(repo, "rev-parse", "--git-common-dir")
    if r.returncode != 0:
        return
    common = Path(r.stdout.strip())
    if not common.is_absolute():
        common = Path(repo) / common
    exclude = common / "info" / "exclude"
    line = f"/{WORKTREES_DIRNAME}/"
    text = exclude.read_text() if exclude.exists() else ""
    if line not in text.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        sep = "" if not text or text.endswith("\n") else "\n"
        exclude.write_text(f"{text}{sep}{line}\n")


def prepare(repo, slug):
    """Path of the task's worktree in `repo`, created or reused."""
    repo = Path(repo).resolve()
    path = repo / WORKTREES_DIRNAME / slug
    branch = BRANCH_PREFIX + slug
    _exclude_worktrees(repo)
    if path.exists():
        if repo_toplevel(path):
            return path
        raise WorkspaceError(f"{path} exists but is not a git worktree")
    # A branch left without its worktree (removed by hand, or by the janitor
    # before a reopened review) is re-attached as is, never rebuilt.
    if _has_ref(repo, f"refs/heads/{branch}"):
        r = _git(repo, "worktree", "add", str(path), branch)
    else:
        try:
            _git(repo, "fetch", "--quiet", "origin", "main", timeout=120)
        except subprocess.TimeoutExpired:
            pass  # offline: branch from the last known origin/main
        base = next((b for b in BASES if _has_ref(repo, b)), None)
        if base is None:
            raise WorkspaceError(f"{repo}: no origin/main or main to branch from")
        r = _git(repo, "worktree", "add", "--no-track", "-b", branch,
                 str(path), base)
    if r.returncode != 0:
        raise WorkspaceError(f"{repo}: git worktree add failed: "
                             f"{r.stderr.strip()}")
    return path


def session_dirs(task_file, backlog_root, dirs):
    """The dirs the session may write to: each repo dir replaced by the
    task's worktree in it, unless the task declares `workdir: main`."""
    workdir = tasks.task_workdir(task_file)
    if workdir is None:
        raise WorkspaceError(f"{task_file}: unknown workdir: value")
    slug = Path(task_file).stem
    backlog = Path(backlog_root).resolve()
    out = []
    for d in dirs:
        if (workdir == tasks.WORKDIR_MAIN or Path(d).resolve() == backlog
                or not repo_toplevel(d)):
            out.append(str(d))
        else:
            out.append(str(prepare(d, slug)))
    return out


def _worktrees(repo):
    """`(path, branch)` of the repo's worktrees; branch is None when detached."""
    r = _git(repo, "worktree", "list", "--porcelain")
    if r.returncode != 0:
        return []
    out, path = [], None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
            out.append((path, None))
        elif line.startswith("branch refs/heads/") and out:
            out[-1] = (path, line[len("branch refs/heads/"):])
    return out


def _pr_state(repo, branch):
    """MERGED, CLOSED, OPEN, or None when there is no PR or gh cannot tell."""
    try:
        r = subprocess.run(["gh", "pr", "view", branch, "--json", "state",
                            "-q", ".state"], cwd=str(repo),
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _finished(repo, branch):
    """"merged" when origin/main contains the branch or its PR was merged
    (a squash merge leaves no ancestry, but the commits live on GitHub),
    "closed" when its PR was closed unmerged, None while still in flight."""
    if (_has_ref(repo, "origin/main") and _git(
            repo, "merge-base", "--is-ancestor", branch,
            "origin/main").returncode == 0):
        return "merged"
    return {"MERGED": "merged", "CLOSED": "closed"}.get(_pr_state(repo, branch))


def reap(dirs):
    """Remove finished task worktrees in the repos among `dirs`: a list of
    `(path, outcome)`, outcome one of "removed" or "dirty"."""
    out = []
    for d in dirs:
        if not repo_toplevel(d):
            continue
        repo = Path(d).resolve()
        agent_dir = repo / WORKTREES_DIRNAME
        candidates = [(path, branch) for path, branch in _worktrees(repo)
                      if path.resolve().parent == agent_dir
                      and branch == BRANCH_PREFIX + path.name]
        if not candidates:
            continue
        try:
            _git(repo, "fetch", "--quiet", "origin", "main", timeout=120)
        except subprocess.TimeoutExpired:
            pass
        for path, branch in candidates:
            finished = _finished(repo, branch)
            if finished is None:
                continue
            if _git(path, "status", "--porcelain").stdout.strip():
                out.append((path, "dirty"))
                continue
            if _git(repo, "worktree", "remove", str(path)).returncode != 0:
                out.append((path, "dirty"))
                continue
            # A merged branch is safe to drop; a closed PR's branch is kept,
            # so nothing committed is ever lost.
            if finished == "merged":
                _git(repo, "branch", "-D", branch)
            out.append((path, "removed"))
    return out


def _main(argv):
    if len(argv) < 4 or argv[1] != "dirs":
        print(__doc__, file=sys.stderr)
        return 2
    task_file, backlog_root, *dirs = argv[2:]
    try:
        for d in session_dirs(task_file, backlog_root, dirs):
            print(d)
    except (WorkspaceError, OSError, subprocess.TimeoutExpired) as e:
        print(f"workspace: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
