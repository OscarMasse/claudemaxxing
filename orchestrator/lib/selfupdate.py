"""Engine self-update: fast-forward the engine's own checkout to origin/main.

Pull-based deployment, once per night (ADR 0075). The caller decides WHEN
(night regime, no RUNNING lock, not already tried tonight); this module decides
WHETHER it is safe and does it: fetch, refuse unless a pure fast-forward on
`main` of a clean checkout, validate the candidate by loading the live config
with the candidate's own lib/config.py, and only then `merge --ff-only`.
It never resets, stashes or forces. Every outcome is a Result the caller can
journal; a refusal leaves the engine on its current SHA.
"""
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Untracked tooling directories that do not make a checkout "dirty".
IGNORED_UNTRACKED = (".serena/", ".claude/", ".agent-worktrees/")


@dataclass
class Result:
    status: str  # updated | current | refused
    old: str = ""
    new: str = ""
    reason: str = ""
    retry: bool = False  # transient failure: the night may try again
    subjects: list = field(default_factory=list)

    def line(self):
        if self.status == "updated":
            brought = "; ".join(self.subjects) or "no commits listed"
            return f"engine updated {self.old[:7]} -> {self.new[:7]}: {brought}"
        if self.status == "current":
            return f"engine already current at {self.old[:7]}"
        return f"engine update refused, staying on {self.old[:7]}: {self.reason}"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _out(repo, *args):
    r = _git(repo, *args)
    return r.stdout.strip() if r.returncode == 0 else None


def _dirty(repo):
    r = _git(repo, "status", "--porcelain")
    for line in r.stdout.splitlines():
        if line.startswith("??") and line[3:].startswith(IGNORED_UNTRACKED):
            continue
        return True
    return r.returncode != 0


def validate(repo, sha, config_path):
    """Error text when `sha` cannot load the live config, else None.

    The candidate tree is exported to a scratch dir and its own lib/config.py
    loads the config: a release that renames a key refuses the old file
    (2026-10-04), and finding that out after the switch crashes every tick."""
    with tempfile.TemporaryDirectory() as tmp:
        tar = subprocess.run(["git", "-C", str(repo), "archive", sha, "orchestrator/lib"],
                             capture_output=True)
        if tar.returncode:
            return f"git archive failed: {tar.stderr.decode().strip()}"
        x = subprocess.run(["tar", "-x", "-C", tmp], input=tar.stdout, capture_output=True)
        if x.returncode:
            return "cannot extract the candidate"
        code = ("import sys; sys.path.insert(0, 'orchestrator'); "
                "from lib import config; config.accounts(config.load(sys.argv[1]))")
        r = subprocess.run([sys.executable, "-I", "-c", code, str(Path(config_path).resolve())],
                           cwd=tmp, capture_output=True, text=True)
        if r.returncode:
            tail = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
            return f"candidate cannot load config.toml: {tail}"
    return None


def update(repo, config_path, remote_url="origin", branch="main"):
    """Fast-forward `repo` to `remote_url`/`branch` when safe; see module doc."""
    old = _out(repo, "rev-parse", "HEAD") or ""

    def refuse(reason):
        return Result("refused", old=old, reason=reason)

    if _out(repo, "symbolic-ref", "--short", "HEAD") != branch:
        return refuse(f"checkout is not on {branch}")
    if _dirty(repo):
        return refuse("checkout has uncommitted changes")
    f = _git(repo, "fetch", "--quiet", remote_url, branch)
    if f.returncode:
        res = refuse(f"fetch failed: {f.stderr.strip()[:200]}")
        res.retry = True
        return res
    new = _out(repo, "rev-parse", "FETCH_HEAD")
    if not new:
        return refuse("fetch left no FETCH_HEAD")
    if new == old:
        return Result("current", old=old, new=new)
    if _git(repo, "merge-base", "--is-ancestor", old, new).returncode:
        ahead = _git(repo, "merge-base", "--is-ancestor", new, old).returncode == 0
        return refuse("local main is ahead of the remote" if ahead
                      else "local main has diverged from the remote")
    problem = validate(repo, new, config_path)
    if problem:
        return refuse(problem)
    subjects = (_out(repo, "log", "--format=%s", f"{old}..{new}") or "").splitlines()
    m = _git(repo, "merge", "--ff-only", "--quiet", new)
    if m.returncode:
        return refuse(f"fast-forward failed: {m.stderr.strip()[:200]}")
    return Result("updated", old=old, new=new, subjects=subjects)


def tonight_marker(state_dir, night_key):
    return Path(state_dir) / f"selfupdate-{night_key}"
