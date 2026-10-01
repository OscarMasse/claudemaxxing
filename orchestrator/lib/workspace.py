"""Give each background session its own git worktree instead of a main checkout.

Why this exists
---------------
On 2026-09-25 a session ran `git checkout -b <branch> origin/main` inside the
webapp main checkout while another session had uncommitted work there. The
switch carried the other session's dirty files into the new branch, and a
`git stash` or `git reset --hard` at the wrong moment would have destroyed
them. Leaving the choice of a worktree to each session's judgement is what
failed, so the launcher makes it instead: run.sh asks this module for the
directories a session may write to, and every repo among the task's project
dirs comes back as a worktree dedicated to that task.

The layout
----------
`<repo>/.agent-worktrees/<task-slug>` on branch `agent/<task-slug>`, created
from `origin/main` (fetched first, best effort). A task that stacks on a
prerequisite whose PR is still open (lib/tasks.py `stack_base`) branches from
that PR's head instead, in the repo the PR belongs to: the session builds its
layer on top and adds its PR to the stack with `gh stack link`. A later slice of the same
task reuses the worktree, or re-attaches the branch when only the branch is
left. The deterministic names are the record: nothing else has to remember
which worktree belongs to which task.

What is not a repo dir
----------------------
Only a dir that is itself the top level of a git repo gets a worktree. The
backlog root is written in place by design (task files, digests), and a
parent dir holding several repos (`~/projects`) names no single repo, so
both are passed through unchanged. A task that must work in the main
checkout itself (webapp seeds in its gitignored `scripts/`) declares
`workdir: main`, validated in lib/tasks.py.

Optional dirs
-------------
A project's `optional_dirs` (lib/config.py) are repos only some of its tasks
touch, like webapp's ~1 GB big-assets checkout. A task that declares one
in `uses:` gets it like any other dir, worktree included; every other
optional dir gets no worktree and reaches the session as its main checkout,
read-only through deny rules (lib/permissions.py readonly_rules).

Cleanup
-------
`reap` (called by gate.py next to the compose janitor) removes a task's
worktree once its branch is merged into origin/main or its PR is merged or
closed, and only when it has no uncommitted or untracked changes; a dirty one
is returned for the log, never deleted. Only worktrees this module creates
are candidates: `.agent-worktrees/<slug>` on `agent/<slug>`.

CLI (run.sh):
  python3 lib/workspace.py session-cwd
prints (and creates) the directory sessions start in, see session_cwd().
  python3 lib/workspace.py dirs <task_file> <backlog_root> <project_workdir> <dir>...
prints one session dir per line, in order.
  python3 lib/workspace.py optional <task_file> <backlog_root> <optional_dir>...
prints `use<TAB><dir>` for each optional dir the task declares in `uses:`
(run.sh adds it to the dirs above) and `readonly<TAB><dir>` for the others.
Both subcommands exit 1, with the reason on
stderr, when a worktree cannot be prepared: the task is set `blocked` with
the reason (and a NEEDS-HUMAN line), and the launcher does not start the
session rather than start it in a main checkout.
"""
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import tasks  # noqa: E402

WORKTREES_DIRNAME = ".agent-worktrees"  # also what lib/janitor.py matches
BRANCH_PREFIX = "agent/"
# Tried in order when origin/HEAD is not set: big-assets' default branch
# is `master`, and a repo without a remote only has its local branch.
BASES = ("origin/main", "origin/master", "main", "master")


def session_cwd():
    """The directory every headless session starts in: outside any repo, so
    a bare `git checkout` that forgot its `-C` fails instead of switching a
    main checkout (sessions used to start in the engine's own checkout).
    gate.py excludes its transcripts from the owner's activity."""
    return Path(os.environ.get("ORCH_SESSION_CWD")
                or "~/.local/state/claudemaxxing/session").expanduser()


def transcripts_dirname(path):
    """Claude Code's projects/ subdir name for a cwd: every character that is
    not alphanumeric becomes a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


class WorkspaceError(RuntimeError):
    pass


def _git(repo, *args, timeout=60):
    # Headless: a fetch that needs credentials it lacks must fail, not wait
    # for a terminal prompt that never comes.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_SSH_COMMAND="ssh -o BatchMode=yes")
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


def _fetch_base(repo):
    """Fetch the remote's default branch (best effort, offline is fine) and
    return the ref to branch from and to judge merges against: origin/HEAD's
    target, else the first of BASES that exists, else None."""
    r = _git(repo, "symbolic-ref", "--quiet", "--short",
             "refs/remotes/origin/HEAD")
    head = r.stdout.strip() if r.returncode == 0 else ""
    candidates = ([head] if head else []) + list(BASES)
    remote = next((b for b in candidates
                   if b.startswith("origin/") and _has_ref(repo, b)), None)
    if remote:
        try:
            _git(repo, "fetch", "--quiet", "origin",
                 remote[len("origin/"):], timeout=120)
        except subprocess.TimeoutExpired:
            pass  # offline: the last known remote state will do
    return next((b for b in candidates if _has_ref(repo, b)), None)


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


def origin_repo(repo):
    """`owner/name` of the repo's `origin` on GitHub, or None."""
    r = _git(repo, "remote", "get-url", "origin", timeout=10)
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$",
                  r.stdout.strip()) if r.returncode == 0 else None
    return m.group(1) if m else None


def _fetch_head(repo, head):
    """Fetch the stack base branch `head` and return `origin/<head>`."""
    try:
        r = _git(repo, "fetch", "--quiet", "origin",
                 f"+refs/heads/{head}:refs/remotes/origin/{head}", timeout=120)
    except subprocess.TimeoutExpired:
        r = None
    if (r is None or r.returncode != 0) and not _has_ref(repo, f"origin/{head}"):
        raise WorkspaceError(f"{repo}: cannot fetch stack base {head}")
    return f"origin/{head}"


def prepare(repo, slug, declared_branch=None, stack_head=None):
    """Path of the task's worktree in `repo`, created or reused.

    `declared_branch` is the task's `branch:` key: work started before this
    module existed lives on a branch of its own name, often in a worktree of
    its own name. When that branch exists in `repo`, its worktree is reused
    wherever it is, or the branch is attached at the standard path.

    `stack_head` is the branch of the open PR the task stacks on: a new
    branch starts from it rather than from the default branch. An existing
    branch is never rebuilt; its session rebases onto the base itself."""
    repo = Path(repo).resolve()
    path = repo / WORKTREES_DIRNAME / slug
    branch = BRANCH_PREFIX + slug
    _exclude_worktrees(repo)
    # A worktree deleted by hand stays registered and blocks `worktree add`.
    _git(repo, "worktree", "prune")
    if (declared_branch and not _has_ref(repo, f"refs/heads/{declared_branch}")
            and _has_ref(repo, f"origin/{declared_branch}")):
        # Pushed from another checkout (a PR branch): take it from the remote.
        _git(repo, "branch", "--no-track", declared_branch,
             f"origin/{declared_branch}")
    if declared_branch and _has_ref(repo, f"refs/heads/{declared_branch}"):
        for wt, b in _worktrees(repo):
            if b == declared_branch:
                if wt.resolve() == repo:
                    raise WorkspaceError(
                        f"{repo}: branch {declared_branch} is checked out in "
                        f"the main checkout; switch the main checkout away "
                        f"from it first")
                return wt
        branch = declared_branch
    if path.exists():
        if repo_toplevel(path):
            return path
        raise WorkspaceError(f"{path} exists but is not a git worktree")
    # A branch left without its worktree (removed by hand, or by the janitor
    # before a reopened review) is re-attached as is, never rebuilt.
    if _has_ref(repo, f"refs/heads/{branch}"):
        r = _git(repo, "worktree", "add", str(path), branch, timeout=900)
    else:
        base = _fetch_head(repo, stack_head) if stack_head else _fetch_base(repo)
        if base is None:
            raise WorkspaceError(f"{repo}: no default branch to branch from "
                                 f"(tried origin/HEAD, {', '.join(BASES)})")
        # Generous: a large repo's checkout can take minutes on a busy disk.
        r = _git(repo, "worktree", "add", "--no-track", "-b", branch,
                 str(path), base, timeout=900)
    if r.returncode != 0:
        # A half-made worktree would pass as reusable on the next slice.
        _git(repo, "worktree", "remove", "--force", str(path))
        raise WorkspaceError(f"{repo}: git worktree add failed: "
                             f"{r.stderr.strip()}")
    return path


def session_dirs(task_file, backlog_root, dirs, project_workdir="worktree"):
    """The dirs the session may write to: each repo dir replaced by the
    task's worktree in it, unless the task or its project (a repo that is not
    code, like ~/notes) declares `workdir: main`."""
    workdir = tasks.task_workdir(task_file)
    if workdir is None:
        raise WorkspaceError(f"{task_file}: unknown workdir: value")
    if project_workdir == tasks.WORKDIR_MAIN:
        workdir = tasks.WORKDIR_MAIN
    slug = Path(task_file).stem
    declared_branch = tasks._frontmatter(Path(task_file)).get("branch")
    base, _ = tasks.stack_base(backlog_root, slug)
    backlog = Path(backlog_root).resolve()
    out, stacked = [], False
    for d in dirs:
        if (workdir == tasks.WORKDIR_MAIN or Path(d).resolve() == backlog
                or not repo_toplevel(d)):
            out.append(str(d))
            continue
        head = base and origin_repo(d) == base["repo"] and base["head"]
        stacked = stacked or bool(head)
        out.append(str(prepare(d, slug, declared_branch, head or None)))
    if base and not stacked:
        raise WorkspaceError(
            f"stacks on {base['url']}, whose repo {base['repo']} is not a "
            f"worktree dir of the task's project")
    return out


def split_optional(task_file, optional_dirs):
    """`(used, readonly)`: the optional dirs the task declares in `uses:`,
    and the rest. A `uses:` name matching none of them raises: the gate
    refuses such a task (tasks.misconfigured), and a session that got here
    anyway must not run read-only in the repo it was written to change."""
    fm = tasks._frontmatter(Path(task_file))
    used = tasks._declared_uses(fm, {"optional_dirs": list(optional_dirs)})
    if used is None:
        raise WorkspaceError(f"{task_file}: uses: {fm.get('uses')} names no "
                             f"optional dir of the project")
    return used, [d for d in optional_dirs if d not in used]


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


def _pr(repo, branch):
    """`(state, head_oid)` of the branch's PR (state MERGED, CLOSED or OPEN),
    or `(None, None)` when there is none or gh cannot tell."""
    try:
        r = subprocess.run(["gh", "pr", "view", branch, "--json",
                            "state,headRefOid", "-q",
                            '.state + " " + .headRefOid'], cwd=str(repo),
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) != 2:
        return None, None
    return parts[0], parts[1]


def _contains(repo, ancestor, ref):
    """True when `ref` contains `ancestor`; False too when either is unknown."""
    return _git(repo, "merge-base", "--is-ancestor", ancestor,
                ref).returncode == 0


def _finished(repo, branch, base):
    """"merged" when the default branch contains the branch, or when its PR
    was merged and the merged head contains every local commit (a squash
    merge leaves no ancestry; a local commit made after the merge keeps the
    task in flight). "closed" when its PR was closed unmerged. None while
    still in flight."""
    if base and _contains(repo, branch, base):
        return "merged"
    state, head = _pr(repo, branch)
    if state == "MERGED" and _contains(repo, branch, head):
        return "merged"
    return "closed" if state == "CLOSED" else None


def _reap_one(repo, path, branch, base):
    finished = _finished(repo, branch, base)
    if finished is None:
        return None
    if _git(path, "status", "--porcelain").stdout.strip():
        return "dirty"
    if _git(repo, "worktree", "remove", str(path)).returncode != 0:
        return "error"
    # Only a merged branch is dropped, and everything on it is merged; a
    # closed PR's branch is kept, so nothing committed is ever lost.
    if finished == "merged":
        _git(repo, "branch", "-D", branch)
    return "removed"


def reap(dirs):
    """Remove finished task worktrees in the repos among `dirs`: a list of
    `(path, outcome)`, outcome one of "removed", "dirty" or "error"."""
    out = []
    for d in dirs:
        if not repo_toplevel(d):
            continue
        repo = Path(d).resolve()
        agent_dir = repo / WORKTREES_DIRNAME
        _git(repo, "worktree", "prune")
        candidates = [(path, branch) for path, branch in _worktrees(repo)
                      if path.resolve().parent == agent_dir
                      and branch == BRANCH_PREFIX + path.name]
        if not candidates:
            continue
        base = _fetch_base(repo)
        for path, branch in candidates:
            try:
                outcome = _reap_one(repo, path, branch, base)
            except subprocess.TimeoutExpired:
                outcome = "error"
            if outcome:
                out.append((path, outcome))
    return out


BLOCKED_NOTE = ("- {date}: the launcher could not prepare this task's worktree, "
                "so no session ran: {error}. Fix the repo state (e.g. a branch "
                "checked out in the main checkout), then set the task back to "
                "`ready`.\n")


def _block(task_file, backlog_root, error):
    """Park the task the gatekeeper already claimed: left `in-progress`, it
    would sit unreported until the stuck-task repair and then fail again."""
    date = datetime.now().strftime("%F")
    tasks.set_status(Path(task_file), "blocked",
                     BLOCKED_NOTE.format(date=date, error=error))
    with open(Path(backlog_root) / "NEEDS-HUMAN.md", "a") as f:
        f.write(f"- [ ] {Path(task_file).name}: worktree could not be "
                f"prepared ({error}) - fix the repo, then set it `ready`\n")


def _main(argv):
    if len(argv) == 2 and argv[1] == "session-cwd":
        d = session_cwd()
        d.mkdir(parents=True, exist_ok=True)
        print(d)
        return 0
    if len(argv) < 4 or argv[1] not in ("dirs", "optional"):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        if argv[1] == "optional":
            task_file, backlog_root, *optional = argv[2:]
            used, readonly = split_optional(task_file, optional)
            out = ([f"use\t{d}" for d in used]
                   + [f"readonly\t{d}" for d in readonly])
        elif len(argv) < 5:
            return 0  # a project without dirs: nothing to swap
        else:
            task_file, backlog_root, project_workdir, *dirs = argv[2:]
            out = session_dirs(task_file, backlog_root, dirs, project_workdir)
    except (WorkspaceError, OSError, subprocess.TimeoutExpired) as e:
        print(f"workspace: {e}", file=sys.stderr)
        try:
            _block(task_file, backlog_root, e)
        except OSError as e2:
            print(f"workspace: could not block the task: {e2}", file=sys.stderr)
        return 1
    for d in out:
        print(d)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
