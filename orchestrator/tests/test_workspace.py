"""lib/workspace.py against real git repos in a temp dir."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import workspace


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(root, name):
    """A clone of a bare `origin` with one commit on main; the clone's own
    main is then moved ahead so origin/main is distinguishable."""
    origin = root / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True)
    seed = root / f"{name}-seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)],
                   check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(seed, "config", k, v)
    git(seed, "checkout", "-q", "-b", "main")
    git(seed, "commit", "-q", "--allow-empty", "-m", "base")
    git(seed, "push", "-q", "origin", "main")
    repo = root / name
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)],
                   check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(repo, "config", k, v)
    return repo


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.backlog = self.root / "backlog"
        (self.backlog / "tasks").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.backlog)], check=True)
        self.repo = make_repo(self.root, "webapp")
        # The owner's checkout: on a feature branch, with uncommitted work.
        git(self.repo, "checkout", "-q", "-b", "owner-feature")
        (self.repo / "wip.txt").write_text("owner's work\n")
        self.task = self.write_task("webapp-fix.md", "")

    def tearDown(self):
        self.tmp.cleanup()

    def write_task(self, name, extra):
        p = self.backlog / "tasks" / name
        p.write_text(f"---\nproject: webapp\nstatus: in-progress\n{extra}---\n")
        return p

    def dirs(self, *dirs, task=None):
        return workspace.session_dirs(task or self.task, self.backlog,
                                      [str(d) for d in dirs])

    def status(self):
        return (git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"),
                git(self.repo, "status", "--short"))

    def test_repo_dir_becomes_a_task_worktree_from_origin_main(self):
        before = self.status()
        out = self.dirs(self.repo, self.backlog)
        wt = self.repo / ".agent-worktrees" / "webapp-fix"
        self.assertEqual(out, [str(wt), str(self.backlog)])
        self.assertNotIn(str(self.repo), out)
        self.assertEqual(git(wt, "rev-parse", "--abbrev-ref", "HEAD"),
                         "agent/webapp-fix")
        self.assertEqual(git(wt, "rev-parse", "HEAD"),
                         git(self.repo, "rev-parse", "origin/main"))
        # The main checkout is untouched: same branch, same dirty file, and
        # the worktrees dir does not even show as untracked.
        self.assertEqual(self.status(), before)

    def test_stacked_task_branches_from_the_base_pr_branch(self):
        seed = self.root / "webapp-seed"
        git(seed, "checkout", "-q", "-b", "agent/low")
        git(seed, "commit", "-q", "--allow-empty", "-m", "lower layer")
        git(seed, "push", "-q", "origin", "agent/low")
        base = {"task": "low", "url": "https://github.com/o/r/pull/1",
                "repo": "o/r", "head": "agent/low"}
        with mock.patch.object(workspace.tasks, "stack_base",
                               return_value=(base, None)), \
                mock.patch.object(workspace, "origin_repo", return_value="o/r"):
            wt = Path(self.dirs(self.repo)[0])
        self.assertEqual(git(wt, "rev-parse", "HEAD"),
                         git(seed, "rev-parse", "agent/low"))
        self.assertEqual(git(wt, "rev-parse", "--abbrev-ref", "HEAD"),
                         "agent/webapp-fix")

    def test_stacked_task_without_the_pr_repo_is_refused(self):
        base = {"task": "low", "url": "https://github.com/o/r/pull/1",
                "repo": "o/r", "head": "agent/low"}
        with mock.patch.object(workspace.tasks, "stack_base",
                               return_value=(base, None)), \
                mock.patch.object(workspace, "origin_repo", return_value="o/x"):
            with self.assertRaises(workspace.WorkspaceError):
                self.dirs(self.repo)

    def test_origin_repo_reads_https_and_ssh_remotes(self):
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git",
                    "https://github.com/o/r"):
            git(self.repo, "remote", "set-url", "origin", url)
            self.assertEqual(workspace.origin_repo(self.repo), "o/r")

    def test_second_slice_reuses_the_worktree_and_its_commits(self):
        wt = Path(self.dirs(self.repo)[0])
        (wt / "f.txt").write_text("x")
        git(wt, "add", "f.txt")
        git(wt, "commit", "-q", "-m", "slice 1")
        head = git(wt, "rev-parse", "HEAD")
        self.assertEqual(self.dirs(self.repo), [str(wt)])
        self.assertEqual(git(wt, "rev-parse", "HEAD"), head)

    def test_branch_without_worktree_is_reattached(self):
        wt = Path(self.dirs(self.repo)[0])
        (wt / "f.txt").write_text("x")
        git(wt, "add", "f.txt")
        git(wt, "commit", "-q", "-m", "slice 1")
        head = git(wt, "rev-parse", "HEAD")
        git(self.repo, "worktree", "remove", str(wt))
        self.assertEqual(self.dirs(self.repo), [str(wt)])
        self.assertEqual(git(wt, "rev-parse", "HEAD"), head)

    def test_workdir_main_keeps_the_main_checkout(self):
        task = self.write_task("seeds.md", "workdir: main\n")
        self.assertEqual(self.dirs(self.repo, task=task), [str(self.repo)])
        self.assertFalse((self.repo / ".agent-worktrees").exists())

    def test_unknown_workdir_is_refused(self):
        task = self.write_task("ghost.md", "workdir: checkout\n")
        with self.assertRaises(workspace.WorkspaceError):
            self.dirs(self.repo, task=task)

    def test_non_repo_and_parent_dirs_pass_through(self):
        plain = self.root / "Personal"
        plain.mkdir()
        # A parent of repos names no single repo, so it is not a worktree
        # candidate either.
        self.assertEqual(self.dirs(plain, self.root), [str(plain), str(self.root)])

    def test_backlog_root_is_never_swapped(self):
        git(self.backlog, "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-q", "--allow-empty", "-m", "x")
        self.assertEqual(self.dirs(self.backlog), [str(self.backlog)])

    def commit_in(self, wt, name="f.txt"):
        (wt / name).write_text("x")
        git(wt, "add", name)
        git(wt, "commit", "-q", "-m", name)

    def reap(self, pr_state=None, head=None):
        """Reap with gh answering `pr_state`, its merged head defaulting to
        the task branch's current tip."""
        if pr_state and head is None:
            head = git(self.repo, "rev-parse", "--verify", "-q",
                       "agent/webapp-fix") if (self.repo / ".agent-worktrees"
                                                / "webapp-fix").exists() else "0" * 40
        with mock.patch.object(workspace, "_pr", return_value=(pr_state, head)):
            return workspace.reap([str(self.repo), str(self.backlog)])

    def test_reap_keeps_an_unmerged_worktree(self):
        wt = Path(self.dirs(self.repo)[0])
        self.commit_in(wt)
        self.assertEqual(self.reap(), [])
        self.assertTrue(wt.exists())

    def test_reap_removes_a_merged_clean_worktree(self):
        wt = Path(self.dirs(self.repo)[0])
        self.commit_in(wt)
        git(wt, "push", "-q", "origin", "agent/webapp-fix:main")
        self.assertEqual(self.reap(), [(wt, "removed")])
        self.assertFalse(wt.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "agent/webapp-fix"), "")
        # The owner's checkout never moved.
        self.assertEqual(self.status()[0], "owner-feature")

    def test_reap_removes_a_closed_pr_worktree_but_keeps_its_branch(self):
        wt = Path(self.dirs(self.repo)[0])
        self.commit_in(wt)
        self.assertEqual(self.reap("CLOSED"), [(wt, "removed")])
        self.assertFalse(wt.exists())
        self.assertNotEqual(git(self.repo, "branch", "--list", "agent/webapp-fix"), "")

    def test_reap_reports_a_dirty_worktree_and_leaves_it(self):
        wt = Path(self.dirs(self.repo)[0])
        (wt / "wip.txt").write_text("uncommitted")
        self.assertEqual(self.reap("MERGED"), [(wt, "dirty")])
        self.assertTrue((wt / "wip.txt").exists())

    def test_reap_keeps_commits_made_after_a_squash_merged_pr(self):
        wt = Path(self.dirs(self.repo)[0])
        self.commit_in(wt, "a.txt")
        merged_head = git(wt, "rev-parse", "HEAD")
        self.commit_in(wt, "b.txt")  # follow-up slice, not pushed
        self.assertEqual(self.reap("MERGED", merged_head), [])
        self.assertTrue(wt.exists())

    def test_default_branch_named_master(self):
        repo = make_repo(self.root, "assets")
        git(repo, "branch", "-q", "-m", "main", "master")
        subprocess.run(["git", "-C", str(self.root / "assets.git"), "branch",
                        "-q", "-m", "main", "master"], check=True)
        git(repo, "fetch", "-q", "--prune", "origin")
        git(repo, "remote", "set-head", "origin", "master")
        self.assertFalse(workspace._has_ref(repo, "origin/main"))
        wt = Path(self.dirs(repo)[0])
        self.assertEqual(git(wt, "rev-parse", "HEAD"),
                         git(repo, "rev-parse", "origin/master"))

    def test_worktree_deleted_by_hand_is_recreated(self):
        import shutil
        wt = Path(self.dirs(self.repo)[0])
        self.commit_in(wt)
        head = git(wt, "rev-parse", "HEAD")
        shutil.rmtree(wt)
        self.assertEqual(self.dirs(self.repo), [str(wt)])
        self.assertEqual(git(wt, "rev-parse", "HEAD"), head)

    def test_declared_branch_reuses_its_existing_worktree(self):
        old = self.repo / ".agent-worktrees" / "webapp-old-name"
        git(self.repo, "worktree", "add", "-q", "-b", "legacy-branch",
            str(old), "origin/main")
        task = self.write_task("webapp-fix.md", "branch: legacy-branch\n")
        self.assertEqual(self.dirs(self.repo, task=task), [str(old)])

    def test_declared_branch_without_worktree_is_attached_at_the_task_path(self):
        git(self.repo, "branch", "legacy-branch", "origin/main")
        task = self.write_task("webapp-fix.md", "branch: legacy-branch\n")
        wt = Path(self.dirs(self.repo, task=task)[0])
        self.assertEqual(wt, self.repo / ".agent-worktrees" / "webapp-fix")
        self.assertEqual(git(wt, "branch", "--show-current"), "legacy-branch")

    def test_declared_branch_only_on_the_remote_is_fetched_into_the_worktree(self):
        git(self.repo, "push", "-q", "origin", "origin/main:refs/heads/pr-branch")
        git(self.repo, "fetch", "-q", "origin")
        task = self.write_task("webapp-fix.md", "branch: pr-branch\n")
        wt = Path(self.dirs(self.repo, task=task)[0])
        self.assertEqual(git(wt, "branch", "--show-current"), "pr-branch")

    def test_declared_branch_in_the_main_checkout_is_refused(self):
        task = self.write_task("webapp-fix.md", "branch: owner-feature\n")
        with self.assertRaises(workspace.WorkspaceError):
            self.dirs(self.repo, task=task)

    def test_reap_ignores_worktrees_it_did_not_create(self):
        other = self.repo / ".agent-worktrees" / "webapp-topic"
        git(self.repo, "worktree", "add", "-q", "-b", "someone-else",
            str(other), "origin/main")
        self.assertEqual(self.reap("MERGED"), [])
        self.assertTrue(other.exists())

    def test_cli_fails_closed(self):
        task = self.write_task("ghost.md", "workdir: checkout\n")
        r = subprocess.run(
            ["python3", str(Path(workspace.__file__)), "dirs", str(task),
             str(self.backlog), "worktree", str(self.repo)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("workdir", r.stderr)
        # The claimed task is parked where the owner sees it, not left
        # in-progress for the stuck-task repair to relaunch into the wall.
        self.assertIn("status: blocked", task.read_text())
        self.assertIn("ghost.md", (self.backlog / "NEEDS-HUMAN.md").read_text())

    def test_undeclared_optional_dir_stays_readonly_without_worktree(self):
        assets = make_repo(self.root, "big-assets")
        self.assertEqual(workspace.split_optional(self.task, [str(assets)]),
                         ([], [str(assets)]))
        self.assertFalse((assets / ".agent-worktrees").exists())

    def test_declared_optional_dir_is_used(self):
        assets = make_repo(self.root, "big-assets")
        task = self.write_task("sprites.md", "uses: [big-assets]\n")
        self.assertEqual(workspace.split_optional(task, [str(assets)]),
                         ([str(assets)], []))

    def test_optional_cli_fails_closed_on_unknown_uses(self):
        task = self.write_task("ghost.md", "uses: [nope]\n")
        r = subprocess.run(
            ["python3", str(Path(workspace.__file__)), "optional", str(task),
             str(self.backlog), str(self.root / "big-assets")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("status: blocked", task.read_text())

    def test_project_workdir_main_keeps_every_main_checkout(self):
        self.assertEqual(workspace.session_dirs(self.task, self.backlog,
                                                [str(self.repo)], "main"),
                         [str(self.repo)])


if __name__ == "__main__":
    unittest.main()
