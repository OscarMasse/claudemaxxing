import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ORCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH))
import manual  # noqa: E402
from lib import ratelimits  # noqa: E402

# Saturday 15:00: daytime, where the gatekeeper never launches anything.
DAY = datetime.fromisoformat("2026-08-15T15:00:00+02:00")

CFG = (
    'night_start = "02:00"\nnight_end = "06:00"\nmorning_guard = "08:30"\n'
    "prereset_burn_hours = 8\nactivity_idle_night_min = 40\n"
    "night_slice_min = 50\nmax_parallel_sessions = 2\nmax_fable_slots = 1\n"
    "est_session_usd = 2.85\n"
    'claude_bin = "/usr/local/bin/claude"\nclaude_model = "sonnet"\n'
    'claude_effort = "low"\n'
    "[[accounts]]\n"
    'name = "personal"\n'
    'claude_config_dir = "~/.claude"\n'
    "reset_weekday = 3\n"
    'reset_time = "05:59"\n'
    'reset_tz = "Europe/Warsaw"\n'
    "[[projects]]\n"
    'name = "side-projects"\n'
    'account = "personal"\n'
    'dirs = ["~/projects"]\n'
    "rank = 10\n"
)


class Machine:
    """Fake run.sh and clock: a launch takes a RUNNING lock like run.sh does,
    and every sleep lets all running sessions finish (and optionally mark
    their task done, to unblock prerequisites)."""

    def __init__(self, root, finish_done=False, step=timedelta(minutes=1)):
        self.state = root / "orchestrator" / "state" / "personal"
        self.root = root
        self.launched = []
        self.now = DAY
        self.step = step
        self.finish_done = finish_done

    def start(self, p, acct, plan, t):
        self.state.mkdir(parents=True, exist_ok=True)
        slot = next(i for i in range(1, 9) if not (self.state / f"RUNNING.{i}").exists())
        (self.state / f"RUNNING.{slot}").write_text(f"1 {time.time()} {t['model']}")
        self.launched.append(Path(t["path"]).stem)

    def sleep(self, _s):
        for lock in self.state.glob("RUNNING*"):
            lock.unlink()
        if self.finish_done:
            for name in self.launched:
                path = self.root / "tasks" / f"{name}.md"
                path.write_text(path.read_text().replace("status: in-progress", "status: done"))
        self.now += self.step

    def clock(self):
        return self.now


class TestManual(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "orchestrator" / "state").mkdir(parents=True)
        (self.root / "tasks").mkdir()
        (self.root / "config.toml").write_text(CFG)
        env = {"ORCH_ROOT": str(self.root), "ORCH_NOW": DAY.isoformat(),
               "ORCH_IDLE_MIN": "0", "ORCH_NO_NOTIFY": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("ORCH_CONFIG", "BACKLOG_ROOT"):
            os.environ.pop(var, None)
        self.p = manual.gate.paths()
        cfg = manual.config.load(self.p["config"])
        self.projs = manual.config.projects(cfg)
        self.acct = manual.config.accounts(cfg)[0]

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, name, priority="medium", model="sonnet", extra=""):
        (self.root / "tasks" / f"{name}.md").write_text(
            f"---\ntitle: {name}\nproject: side-projects\nstatus: ready\n"
            f"priority: {priority}\nmodel: {model}\ndelivery: branch\n"
            f"created: 2026-08-01\n{extra}---\n\n## Notes\n")

    def status(self, name):
        return manual.tasks._frontmatter(self.root / "tasks" / f"{name}.md")["status"]

    def plan(self, *argv):
        return manual.build_plan(manual.parse_args(list(argv)), self.acct, DAY)

    def run_plan(self, plan, machine=None):
        m = machine or Machine(self.root)
        reason = manual.run(self.p, self.acct, self.projs, plan, m.clock, m.sleep,
                            start=m.start)
        return m, reason

    def log(self):
        return self.p["log"].read_text()

    def test_count_runs_in_daytime_in_queue_order_and_claims(self):
        self.task("a", "high")
        self.task("b", "medium")
        self.task("c", "low")
        self.task("d", "low")
        m, reason = self.run_plan(self.plan("--count", "3"))
        self.assertEqual(m.launched, ["a", "b", "c"])
        self.assertEqual(reason, "count reached")
        self.assertEqual([self.status(n) for n in "abcd"],
                         ["in-progress"] * 3 + ["ready"])
        self.assertIn("manual end launched=3", self.log())

    def test_parallel_ceiling_counts_every_running_session(self):
        for n in "abc":
            self.task(n)
        state = self.p["state"] / "personal"
        state.mkdir(parents=True)
        # A night session already running holds one of the two slots.
        (state / "RUNNING.1").write_text(f"1 {time.time()} sonnet")
        picked = manual.select(self.p, self.acct, self.projs, self.plan("--count", "3"), DAY)
        self.assertEqual(len(picked), 1)
        picked = manual.select(self.p, self.acct, self.projs,
                               self.plan("--count", "3", "--parallel", "4"), DAY)
        self.assertEqual(len(picked), 3)

    def test_fable_slots_pace_fable_only(self):
        self.task("f1", "high", "fable")
        self.task("f2", "high", "fable")
        self.task("s1", "low")
        plan = self.plan("--count", "3")
        picked = manual.select(self.p, self.acct, self.projs, plan, DAY)
        self.assertEqual([Path(t["path"]).stem for t in picked], ["f1", "s1"])
        plan = self.plan("--count", "3", "--fable-slots", "2", "--parallel", "3")
        picked = manual.select(self.p, self.acct, self.projs, plan, DAY)
        self.assertEqual(len([t for t in picked if t["model"] == "fable"]), 2)

    def test_refused_model_gets_no_slot(self):
        self.task("o1", "high", "opus")
        self.task("s1", "low")
        until = DAY + timedelta(hours=2)
        with mock.patch.object(manual.quota, "blocked", return_value={"opus": until}):
            picked = manual.select(self.p, self.acct, self.projs, self.plan("--count", "2"), DAY)
        self.assertEqual([Path(t["path"]).stem for t in picked], ["s1"])

    def test_task_list_keeps_order_drops_unlaunchable_waits_on_listed_prereq(self):
        self.task("first", "low")
        self.task("second", "high")
        self.task("after-first", "high", extra="prerequisites: first\n")
        self.task("orphan-dep", "high", extra="prerequisites: never-listed\n")
        self.task("never-listed")
        (self.root / "tasks" / "blocked.md").write_text(
            "---\ntitle: x\nproject: side-projects\nstatus: blocked\ndelivery: branch\n---\n")
        m, reason = self.run_plan(
            self.plan("--tasks", "after-first", "first.md", "blocked", "orphan-dep",
                      "second", "--parallel", "1"),
            Machine(self.root, finish_done=True))
        # after-first is listed before second: it goes first once first is done.
        self.assertEqual(m.launched, ["first", "after-first", "second"])
        self.assertEqual(reason, "task list launched")
        log = self.log()
        self.assertIn("manual dropped task=blocked: status=blocked", log)
        self.assertIn("manual dropped task=orphan-dep: prerequisites not done", log)
        self.assertEqual(self.status("never-listed"), "ready")

    def test_named_duty_does_not_consume_its_period(self):
        self.task("routine", extra="duty: weekly\n")
        m, _ = self.run_plan(self.plan("--tasks", "routine"))
        self.assertEqual(m.launched, ["routine"])
        self.assertEqual(manual.gate.duties_served(self.p["state"] / "personal"), {})
        self.assertEqual(self.status("routine"), "ready")

    def test_budget_stops_launches(self):
        for n in "abc":
            self.task(n)
        m, reason = self.run_plan(self.plan("--count", "3", "--budget", "6"))
        self.assertEqual(m.launched, ["a", "b"])
        self.assertIn("budget left", reason)

    def test_duration_stops_launching_at_deadline(self):
        for i in range(10):
            self.task(f"t{i}")
        m, reason = self.run_plan(self.plan("--for", "2m", "--parallel", "1"))
        self.assertEqual(reason, "duration elapsed")
        self.assertEqual(len(m.launched), 2)

    def test_empty_queue_ends_the_run(self):
        self.task("only")
        m, reason = self.run_plan(self.plan("--for", "5h"))
        self.assertEqual(m.launched, ["only"])
        self.assertEqual(reason, "nothing left to launch")

    def test_kill_switch_refuses_start_and_stops_a_running_loop(self):
        self.task("a")
        self.p["paused"].touch()
        with mock.patch("sys.stderr"):
            self.assertEqual(manual.main(["--count", "1"]), 1)
        m, reason = self.run_plan(self.plan("--count", "1"))
        self.assertEqual(m.launched, [])
        self.assertIn("kill switch", reason)

    def test_unknown_task_is_refused_in_the_foreground(self):
        self.task("a")
        with mock.patch("sys.stderr"):
            self.assertEqual(manual.main(["--tasks", "a", "nope", "--check"]), 2)
        self.assertEqual(manual.main(["--tasks", "a", "--check"]), 0)

    def test_list_with_nothing_launchable_says_so(self):
        (self.root / "tasks" / "b.md").write_text(
            "---\ntitle: x\nproject: side-projects\nstatus: blocked\ndelivery: branch\n---\n")
        m, reason = self.run_plan(self.plan("--tasks", "b"))
        self.assertEqual(m.launched, [])
        self.assertEqual(reason, "nothing launched")

    def test_duration_parsing(self):
        self.assertEqual(manual.parse_duration("5h"), timedelta(hours=5))
        self.assertEqual(manual.parse_duration("1h30m"), timedelta(minutes=90))
        for bad in ("", "5", "5x", "h"):
            with self.assertRaises(ValueError):
                manual.parse_duration(bad)

    def test_dry_run_launches_nothing(self):
        self.task("a")
        with mock.patch("sys.stdout"):
            self.assertEqual(manual.main(["--count", "1", "--dry-run"]), 0)
        self.assertEqual(self.status("a"), "ready")
        self.assertFalse(self.p["log"].exists())

    def test_daytime_gatekeeper_leaves_manual_sessions_alone(self):
        self.task("a")
        state = self.p["state"] / "personal"
        state.mkdir(parents=True)
        lock = state / "RUNNING.1"
        lock.write_text(f"1 {time.time()} sonnet")
        fx = self.root / "usage.json"
        fx.write_text('{"week_usd": 0, "week_by_family": {}, "block": null, "unknown_models": []}')
        ratelimits.seed(state, 100, 15, 10, "2026-08-01T00:00:00+00:00")
        env = dict(os.environ, ORCH_USAGE_JSON=str(fx), ORCH_DOCKER_BIN="")
        r = subprocess.run(["python3", str(ORCH / "gate.py"), "tick"], cwd=ORCH,
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "SKIP personal day: daytime runs need manual.sh",
                         r.stdout + r.stderr)
        self.assertTrue(lock.exists())
        self.assertEqual(self.status("a"), "ready")


if __name__ == "__main__":
    unittest.main()
