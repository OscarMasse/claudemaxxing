import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ORCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH))
from lib import janitor  # noqa: E402


class TestReapable(unittest.TestCase):
    def test_only_agent_worktrees_inside_project_dirs(self):
        rows = [
            ("webapp-a", "/p/webapp/.agent-worktrees/webapp-a"),
            ("webapp-a", "/p/webapp/.agent-worktrees/webapp-a"),  # 2nd container
            ("webapp", "/p/webapp"),                               # owner checkout
            ("mine", "/p/webapp/.worktrees/mine"),                # owner worktree
            ("work", "/w/core/.agent-worktrees/x"),              # other root
            ("", ""),                                            # not compose
        ]
        self.assertEqual(janitor.reapable(rows, ["/p"]), ["webapp-a"])

    def test_no_project_dirs_reaps_nothing(self):
        rows = [("webapp-a", "/p/webapp/.agent-worktrees/webapp-a")]
        self.assertEqual(janitor.reapable(rows, []), [])


class TestSweep(unittest.TestCase):
    """Drives a stub docker CLI: it prints canned `ps` rows and records every
    other invocation, so the real daemon is never involved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.calls = self.dir / "calls"
        rows = ("webapp-a\t/p/webapp/.agent-worktrees/webapp-a\n"
                "webapp\t/p/webapp\n")
        self.left = self.dir / "left"   # IDs `ps -aq --filter` reports
        self.rm_works = self.dir / "rm_works"
        self.rm_works.touch()
        stub = self.dir / "docker"
        stub.write_text(
            "#!/bin/sh\n"
            f"if [ \"$1 $2\" = 'ps -a' ]; then printf '{rows}'; exit 0; fi\n"
            f"echo \"$@\" >> {self.calls}\n"
            f"if [ \"$1\" = ps ]; then cat {self.left} 2>/dev/null; fi\n"
            f"if [ \"$1\" = rm ] && [ -e {self.rm_works} ]; then"
            f" rm -f {self.left}; fi\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.stub = str(stub)

    def tearDown(self):
        self.tmp.cleanup()

    def test_downs_agent_stacks_only(self):
        with mock.patch.dict(os.environ, {"ORCH_DOCKER_BIN": self.stub}):
            self.assertEqual(janitor.sweep(["/p"]), [("webapp-a", True)])
        self.assertEqual(self.calls.read_text(),
                         "compose -p webapp-a down --remove-orphans\n"
                         "ps -aq --filter label=com.docker.compose.project="
                         "webapp-a\n")

    def test_removes_run_container_down_left_behind(self):
        # A one-off `compose run` container in state Created survives `down`.
        self.left.write_text("c1f90a1bcc65\n")
        with mock.patch.dict(os.environ, {"ORCH_DOCKER_BIN": self.stub}):
            self.assertEqual(janitor.sweep(["/p"]), [("webapp-a", True)])
        calls = self.calls.read_text().splitlines()
        self.assertIn("rm -f c1f90a1bcc65", calls)
        self.assertFalse(any("-v" in c.split() or "volume" in c
                             for c in calls))
        self.assertFalse(self.left.exists())

    def test_surviving_container_is_a_failure(self):
        self.left.write_text("c1f90a1bcc65\n")
        self.rm_works.unlink()
        with mock.patch.dict(os.environ, {"ORCH_DOCKER_BIN": self.stub}):
            self.assertEqual(janitor.sweep(["/p"]), [("webapp-a", False)])

    def test_disabled_by_empty_bin(self):
        with mock.patch.dict(os.environ, {"ORCH_DOCKER_BIN": ""}):
            self.assertEqual(janitor.sweep(["/p"]), [])

    def test_missing_docker_is_not_an_error(self):
        with mock.patch.dict(os.environ,
                             {"ORCH_DOCKER_BIN": str(self.dir / "nope")}):
            self.assertEqual(janitor.sweep(["/p"]), [])


if __name__ == "__main__":
    unittest.main()
