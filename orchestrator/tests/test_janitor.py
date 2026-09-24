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
            ("rankr-a", "/p/rankr/.agent-worktrees/rankr-a"),
            ("rankr-a", "/p/rankr/.agent-worktrees/rankr-a"),  # 2nd container
            ("rankr", "/p/rankr"),                               # owner checkout
            ("mine", "/p/rankr/.worktrees/mine"),                # owner worktree
            ("work", "/w/core/.agent-worktrees/x"),              # other root
            ("", ""),                                            # not compose
        ]
        self.assertEqual(janitor.reapable(rows, ["/p"]), ["rankr-a"])

    def test_no_project_dirs_reaps_nothing(self):
        rows = [("rankr-a", "/p/rankr/.agent-worktrees/rankr-a")]
        self.assertEqual(janitor.reapable(rows, []), [])


class TestSweep(unittest.TestCase):
    """Drives a stub docker CLI: it prints canned `ps` rows and records every
    other invocation, so the real daemon is never involved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.calls = self.dir / "calls"
        rows = ("rankr-a\t/p/rankr/.agent-worktrees/rankr-a\n"
                "rankr\t/p/rankr\n")
        stub = self.dir / "docker"
        stub.write_text(
            "#!/bin/sh\n"
            f"if [ \"$1\" = ps ]; then printf '{rows}'; exit 0; fi\n"
            f"echo \"$@\" >> {self.calls}\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.stub = str(stub)

    def tearDown(self):
        self.tmp.cleanup()

    def test_downs_agent_stacks_only(self):
        with mock.patch.dict(os.environ, {"ORCH_DOCKER_BIN": self.stub}):
            self.assertEqual(janitor.sweep(["/p"]), [("rankr-a", True)])
        self.assertEqual(self.calls.read_text(),
                         "compose -p rankr-a down --remove-orphans\n")

    def test_disabled_by_empty_bin(self):
        with mock.patch.dict(os.environ, {"ORCH_DOCKER_BIN": ""}):
            self.assertEqual(janitor.sweep(["/p"]), [])

    def test_missing_docker_is_not_an_error(self):
        with mock.patch.dict(os.environ,
                             {"ORCH_DOCKER_BIN": str(self.dir / "nope")}):
            self.assertEqual(janitor.sweep(["/p"]), [])


if __name__ == "__main__":
    unittest.main()
