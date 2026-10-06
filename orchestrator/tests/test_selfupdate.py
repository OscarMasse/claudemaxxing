import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lib import selfupdate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = REPO_ROOT / "orchestrator" / "config.toml"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


class UpdateCase(unittest.TestCase):
    """A bare 'origin', an engine clone of it and a second clone to push from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.origin, self.engine, self.dev = t / "o.git", t / "engine", t / "dev"
        git(t, "init", "--bare", "-b", "main", str(self.origin))
        git(t, "clone", "-q", str(self.origin), str(self.dev))
        shutil.copytree(REPO_ROOT / "orchestrator" / "lib", self.dev / "orchestrator" / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-qm", "base")
        git(self.dev, "push", "-q", "origin", "main")
        git(t, "clone", "-q", str(self.origin), str(self.engine))

    def tearDown(self):
        self.tmp.cleanup()

    def push(self, msg="feature", config_py=None):
        if config_py is not None:
            (self.dev / "orchestrator" / "lib" / "config.py").write_text(config_py)
        (self.dev / f"{msg}.txt").write_text(msg)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-qm", msg)
        git(self.dev, "push", "-q", "origin", "main")

    def run_update(self):
        return selfupdate.update(self.engine, CONFIG)


class TestUpdate(UpdateCase):
    def test_current_when_nothing_new(self):
        self.assertEqual(self.run_update().status, "current")

    def test_fast_forwards_and_lists_what_it_brought(self):
        self.push("one")
        self.push("two")
        res = self.run_update()
        self.assertEqual(res.status, "updated")
        self.assertEqual(res.subjects, ["two", "one"])
        self.assertTrue((self.engine / "two.txt").exists())
        self.assertNotEqual(res.old, res.new)

    def test_dirty_checkout_refused(self):
        self.push()
        (self.engine / "orchestrator" / "lib" / "config.py").write_text("# edited\n")
        res = self.run_update()
        self.assertEqual(res.status, "refused")
        self.assertIn("uncommitted", res.reason)
        self.assertFalse((self.engine / "feature.txt").exists())

    def test_untracked_tooling_dir_is_not_dirty(self):
        (self.engine / ".serena").mkdir()
        (self.engine / ".serena" / "x").write_text("x")
        self.push()
        self.assertEqual(self.run_update().status, "updated")

    def test_other_untracked_file_is_dirty(self):
        (self.engine / "stray.txt").write_text("x")
        self.assertEqual(self.run_update().status, "refused")

    def test_non_main_branch_refused(self):
        git(self.engine, "checkout", "-q", "-b", "topic")
        self.push()
        res = self.run_update()
        self.assertEqual(res.status, "refused")
        self.assertIn("not on main", res.reason)

    def test_diverged_refused(self):
        (self.engine / "local.txt").write_text("l")
        git(self.engine, "add", "-A")
        git(self.engine, "commit", "-qm", "local")
        self.push()
        res = self.run_update()
        self.assertEqual(res.status, "refused")
        self.assertIn("diverged", res.reason)

    def test_fetch_failure_refused(self):
        git(self.engine, "remote", "set-url", "origin", str(Path(self.tmp.name) / "gone"))
        res = self.run_update()
        self.assertEqual(res.status, "refused")
        self.assertIn("fetch failed", res.reason)
        self.assertTrue(res.retry)

    def test_candidate_that_cannot_load_config_refused(self):
        old = self.run_update().old
        self.push("bad", config_py="raise RuntimeError('boom')\n")
        res = self.run_update()
        self.assertEqual(res.status, "refused")
        self.assertIn("cannot load config.toml", res.reason)
        head = subprocess.run(["git", "-C", str(self.engine), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, old)


class TestDue(unittest.TestCase):
    """gate.selfupdate_due: night regime, no RUNNING lock, once per night."""

    def setUp(self):
        import gate
        self.gate = gate
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        (self.state / "max").mkdir()
        self.p = {"state": self.state, "log": self.state / "g.log"}
        self.acct = {"name": "max", "night_start": "02:00", "night_end": "07:00"}

    def tearDown(self):
        self.tmp.cleanup()

    def due(self, hour):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 6, hour, 0, tzinfo=timezone.utc)
        return self.gate.selfupdate_due(self.p, [self.acct], lambda _a: now)

    def test_due_inside_night_when_idle(self):
        self.assertEqual(self.due(3), "2026-10-06")

    def test_not_due_by_day(self):
        self.assertIsNone(self.due(12))

    def test_deferred_while_a_session_runs(self):
        import time
        (self.state / "max" / "RUNNING.1").write_text(f"1 {time.time()}\n")
        self.assertIsNone(self.due(3))

    def test_once_per_night(self):
        selfupdate.tonight_marker(self.state, "2026-10-06").write_text("x")
        self.assertIsNone(self.due(3))


class TestLoopReexec(unittest.TestCase):
    def test_loop_execs_itself_after_an_update(self):
        text = (REPO_ROOT / "orchestrator" / "gatekeeper-loop.sh").read_text()
        self.assertIn('= "UPDATED" ]', text)
        self.assertIn('exec /bin/bash "$SELF"', text)
        self.assertLess(text.index("gate.py selfupdate"), text.index("/bin/bash gatekeeper.sh"))


if __name__ == "__main__":
    unittest.main()
