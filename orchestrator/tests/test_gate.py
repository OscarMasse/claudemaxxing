import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ORCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH))
import gate  # noqa: E402

FIXTURE = {"blocks": [
    {"startTime": "2026-08-07T08:00:00.000Z", "endTime": "2026-08-07T13:00:00.000Z",
     "isActive": False, "isGap": False, "totalTokens": 40},
]}

# Shared knobs are flat; calibration lives in the account. reset_weekday 3
# makes Wednesday 2026-08-12 the late-week surplus scenario from the
# controller tests.
BASE_CFG = (
    "dry_run: false\n"
    "night_start: 02:00\nnight_end: 06:00\nmorning_guard: 08:30\n"
    "prereset_burn_hours: 8\nactivity_idle_day_min: 60\nactivity_idle_night_min: 40\n"
    "day_slice_min: 15\nnight_slice_min: 50\nday_window_max_frac: 0.4\n"
    "claude_bin: /usr/local/bin/claude\nclaude_model: sonnet\nclaude_effort: low\n"
)

PERSONAL = (
    "accounts:\n"
    "  - name: personal\n"
    "    claude_config_dir: ~/.claude\n"
    "    weekly_cap_tokens: 100\n"
    "    window_cap_tokens: 15\n"
    "    p90_daily_tokens: 10\n"
    "    reset_weekday: 3\n"
    "    reset_time: 05:59\n"
    "    reset_tz: Europe/Warsaw\n"
)

PROJECTS = (
    "projects:\n"
    "  - name: side-projects\n"
    "    account: personal\n"
    "    dirs: ~/projects\n"
    "    priority: 10\n"
)


def run_gate(root, extra_env=None, arg="tick"):
    env = dict(os.environ,
               ORCH_ROOT=str(root),
               ORCH_NOW="2026-08-12T15:00:00+02:00",  # Wednesday afternoon
               ORCH_IDLE_MIN="999",
               ORCH_NO_NOTIFY="1")
    # A developer shell may carry these; they must not leak into the tests.
    env.pop("ORCH_CONFIG", None)
    env.pop("BACKLOG_ROOT", None)
    env.update(extra_env or {})
    return subprocess.run(["python3", str(ORCH / "gate.py"), arg],
                          capture_output=True, text=True, env=env, cwd=ORCH)


class TestGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "orchestrator" / "state").mkdir(parents=True)
        (self.root / "tasks").mkdir()
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n---\n")
        fx = self.root / "fixture.json"
        fx.write_text(json.dumps(FIXTURE))
        self.env = {"ORCH_CCUSAGE_JSON": str(fx)}
        self.write_cfg(BASE_CFG + PERSONAL + PROJECTS)

    def tearDown(self):
        self.tmp.cleanup()

    def write_cfg(self, text):
        (self.root / "config.yml").write_text(text)

    def append_cfg(self, text):
        """Append FLAT keys: they act as shared defaults for every account."""
        cfg = self.root / "config.yml"
        cfg.write_text(cfg.read_text() + text)

    def write_task(self, name, text):
        (self.root / "tasks" / name).write_text(text)

    def test_paused_wins(self):
        (self.root / "orchestrator" / "PAUSED").touch()
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP paused"), r.stdout)

    def test_run_decision_and_log(self):
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("RUN personal 15"), r.stdout + r.stderr)
        self.assertIn("t1.md sonnet low side-projects", r.stdout)
        log = (self.root / "orchestrator" / "state" / "gatekeeper.log").read_text()
        self.assertIn("account=personal", log)
        self.assertIn("day surplus regime", log)

    def test_opus_task_not_picked_during_day(self):
        # Make the only task an opus one: daytime surplus must not launch it.
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: opus\n---\n")
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal no eligible task"), r.stdout)

    def test_opus_task_picked_at_night(self):
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: opus\neffort: medium\n---\n")
        env = dict(self.env, ORCH_NOW="2026-08-11T02:30:00+02:00")  # Tuesday night
        r = run_gate(self.root, env)
        self.assertTrue(r.stdout.startswith("RUN personal 50"), r.stdout)
        self.assertIn("opus medium", r.stdout)

    def test_prereset_upgrades_to_fable_with_margin(self):
        self.append_cfg("fable_min_surplus_tokens: 50\nopus_min_surplus_tokens: 20\n")
        env = dict(self.env, ORCH_NOW="2026-08-12T23:00:00+02:00")  # <8h to reset
        r = run_gate(self.root, env)
        self.assertTrue(r.stdout.startswith("RUN"), r.stdout + r.stderr)
        self.assertIn("t1.md fable", r.stdout)  # sonnet floor upgraded

    def test_prereset_small_margin_stays_sonnet(self):
        self.append_cfg("fable_min_surplus_tokens: 5000\nopus_min_surplus_tokens: 4000\n")
        env = dict(self.env, ORCH_NOW="2026-08-12T23:00:00+02:00")
        r = run_gate(self.root, env)
        self.assertTrue(r.stdout.startswith("RUN"), r.stdout + r.stderr)
        self.assertIn("t1.md sonnet", r.stdout)

    def test_no_eligible_task(self):
        (self.root / "tasks" / "t1.md").unlink()
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal no eligible task"), r.stdout)

    def test_parallel_slots_at_night(self):
        # Cheap estimated rate: 3 sessions fit the night budget (15 tokens).
        self.append_cfg("night_parallel: 3\nday_parallel: 1\n"
                        "est_rate_sonnet_per_min: 0.05\n")
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "parallel: true\n---\n")
        env = dict(self.env, ORCH_NOW="2026-08-11T02:30:00+02:00")
        r = run_gate(self.root, env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len(lines), 3, r.stdout)  # one parallel task fills 3 slots

    def test_heavy_task_limits_parallelism(self):
        # One session's estimated burn (1.0 x 50min = 50) exceeds the night
        # budget (15): parallelizing is pointless, exactly one session runs.
        self.append_cfg("night_parallel: 3\nday_parallel: 1\n"
                        "est_rate_sonnet_per_min: 1.0\n")
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "parallel: true\n---\n")
        env = dict(self.env, ORCH_NOW="2026-08-11T02:30:00+02:00")
        r = run_gate(self.root, env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len(lines), 1, r.stdout)

    def test_parallel_respects_active_slots(self):
        self.append_cfg("night_parallel: 2\nday_parallel: 1\n")
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        (state / "RUNNING.1").write_text(f"999 {int(time.time())}")
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "parallel: true\n---\n")
        env = dict(self.env, ORCH_NOW="2026-08-11T02:30:00+02:00")
        r = run_gate(self.root, env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len(lines), 1, r.stdout)  # 2 slots - 1 active = 1 launch

    def test_dry_run_suppresses(self):
        cfg = self.root / "config.yml"
        cfg.write_text(cfg.read_text().replace("dry_run: false", "dry_run: true"))
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal dry_run"), r.stdout)

    def test_orch_config_env_wins_over_backlog_config(self):
        alt = self.root / "alt-config.yml"
        alt.write_text((self.root / "config.yml").read_text()
                       .replace("dry_run: false", "dry_run: true"))
        r = run_gate(self.root, dict(self.env, ORCH_CONFIG=str(alt)))
        self.assertTrue(r.stdout.startswith("SKIP personal dry_run"), r.stdout)

    def test_backlog_root_env_drives_config_and_state_paths(self):
        # No ORCH_ROOT: BACKLOG_ROOT alone must select the external root's
        # config.yml, tasks/ and state/.
        env = dict(os.environ,
                   BACKLOG_ROOT=str(self.root),
                   ORCH_NOW="2026-08-12T15:00:00+02:00",
                   ORCH_IDLE_MIN="999",
                   ORCH_NO_NOTIFY="1")
        env.pop("ORCH_ROOT", None)
        env.pop("ORCH_CONFIG", None)
        env.update(self.env)
        r = subprocess.run(["python3", str(ORCH / "gate.py"), "tick"],
                           capture_output=True, text=True, env=env, cwd=ORCH)
        self.assertTrue(r.stdout.startswith("RUN personal 15"),
                        r.stdout + r.stderr)
        self.assertIn(str(self.root / "tasks" / "t1.md"), r.stdout)
        log = self.root / "orchestrator" / "state" / "gatekeeper.log"
        self.assertTrue(log.exists())
        self.assertIn("account=personal", log.read_text())

    def test_stale_lock_broken_fresh_lock_respected(self):
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        lock = state / "RUNNING.1"
        lock.write_text("999 0")  # epoch 0 -> stale
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("RUN"), r.stdout)
        self.assertFalse(lock.exists())
        lock.write_text(f"999 {int(time.time())}")  # fresh
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal running"), r.stdout)

    def test_status_prints_summary(self):
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("account=personal", r.stdout)
        self.assertIn("week_tokens", r.stdout)
        self.assertIn("available", r.stdout)

    def test_status_shows_blocked_task_with_unmet_prerequisites(self):
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "prerequisites: dep\n---\n")
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("blocked task=t1.md unmet=dep", r.stdout)

    def test_notifications_marked_once(self):
        (self.root / "NEEDS-HUMAN.md").write_text(
            "# Needs human\n\n- [ ] task-x: which color?\n")
        env = dict(self.env, ORCH_IDLE_MIN="2")  # present at the PC
        run_gate(self.root, env)
        notified = (self.root / "orchestrator" / "state" / "notified.txt").read_text()
        self.assertEqual(len(notified.strip().splitlines()), 1)
        run_gate(self.root, env)  # second tick: no duplicate mark
        notified2 = (self.root / "orchestrator" / "state" / "notified.txt").read_text()
        self.assertEqual(notified, notified2)


class TestPlatformSeam(unittest.TestCase):
    """Platform detection and the notification kill switch."""

    def test_darwin_maps_to_macos(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(gate.sys, "platform", "darwin"):
            self.assertEqual(gate.platform_name(), "macos")

    def test_unknown_platform_passes_through(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(gate.sys, "platform", "linux"):
            self.assertEqual(gate.platform_name(), "linux")

    def test_orch_platform_overrides_detection(self):
        with mock.patch.dict(os.environ, {"ORCH_PLATFORM": "testos"}):
            self.assertEqual(gate.platform_name(), "testos")

    def test_no_notify_env_and_legacy_name(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(gate.notifications_disabled())
        with mock.patch.dict(os.environ, {"ORCH_NO_NOTIFY": "1"}, clear=True):
            self.assertTrue(gate.notifications_disabled())
        # Pre-seam name, kept for one release.
        with mock.patch.dict(os.environ, {"ORCH_NO_OSASCRIPT": "1"}, clear=True):
            self.assertTrue(gate.notifications_disabled())


class TestGateMultiAccount(unittest.TestCase):
    """Two accounts scheduled in the same tick, fully isolated."""

    WORK = (
        "  - name: work\n"
        "    claude_config_dir: ~/.claude-work\n"
        "    weekly_cap_tokens: 100\n"
        "    window_cap_tokens: 15\n"
        "    p90_daily_tokens: 10\n"
        "    reset_weekday: 3\n"
        "    reset_time: 05:59\n"
        "    reset_tz: Europe/Warsaw\n"
    )
    WORK_PROJECT = (
        "  - name: job\n"
        "    account: work\n"
        "    dirs: ~/work\n"
        "    priority: 10\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "orchestrator" / "state").mkdir(parents=True)
        (self.root / "tasks").mkdir()
        (self.root / "config.yml").write_text(
            BASE_CFG + PERSONAL + self.WORK + PROJECTS + self.WORK_PROJECT)
        (self.root / "tasks" / "p.md").write_text(
            "---\ntitle: P\nproject: side-projects\nstatus: ready\n"
            "priority: high\ncreated: 2026-08-01\n---\n")
        (self.root / "tasks" / "w.md").write_text(
            "---\ntitle: W\nproject: job\nstatus: ready\n"
            "priority: high\ncreated: 2026-08-01\n---\n")
        fx = self.root / "fixture.json"
        fx.write_text(json.dumps(FIXTURE))
        self.env = {"ORCH_CCUSAGE_JSON": str(fx)}

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self, r):
        return [l for l in r.stdout.splitlines() if l]

    def test_both_accounts_run_in_one_tick(self):
        r = run_gate(self.root, self.env)
        lines = self.lines(r)
        self.assertEqual(len(lines), 2, r.stdout + r.stderr)
        self.assertIn("RUN personal 15", lines[0])
        self.assertIn("p.md sonnet low side-projects", lines[0])
        self.assertIn("RUN work 15", lines[1])
        self.assertIn("w.md sonnet low job", lines[1])

    def test_budget_isolation(self):
        # The work account's week is exhausted; personal still runs.
        heavy = self.root / "heavy.json"
        heavy.write_text(json.dumps({"blocks": [
            {"startTime": "2026-08-07T08:00:00.000Z",
             "endTime": "2026-08-07T13:00:00.000Z",
             "isActive": False, "isGap": False, "totalTokens": 999},
        ]}))
        env = dict(self.env, ORCH_CCUSAGE_JSON_WORK=str(heavy))
        r = run_gate(self.root, env)
        lines = self.lines(r)
        self.assertTrue(lines[0].startswith("RUN personal"), r.stdout)
        self.assertTrue(lines[1].startswith("SKIP work"), r.stdout)
        self.assertIn("available", lines[1])

    def test_activity_lock_is_per_account(self):
        # The owner is typing on the personal account right now; only the
        # personal account is blocked, work still runs.
        env = dict(self.env, ORCH_IDLE_MIN_PERSONAL="5")
        r = run_gate(self.root, env)
        lines = self.lines(r)
        self.assertTrue(lines[0].startswith("SKIP personal"), r.stdout)
        self.assertIn("activity", lines[0])
        self.assertTrue(lines[1].startswith("RUN work"), r.stdout)

    def test_running_lock_is_per_account(self):
        state = self.root / "orchestrator" / "state" / "work"
        state.mkdir(parents=True)
        (state / "RUNNING.1").write_text(f"999 {int(time.time())}")
        r = run_gate(self.root, self.env)
        lines = self.lines(r)
        self.assertTrue(lines[0].startswith("RUN personal"), r.stdout)
        self.assertEqual(lines[1], "SKIP work running", r.stdout)

    def test_tasks_never_cross_accounts(self):
        (self.root / "tasks" / "p.md").unlink()
        r = run_gate(self.root, self.env)
        lines = self.lines(r)
        self.assertTrue(lines[0].startswith("SKIP personal no eligible task"),
                        r.stdout)
        self.assertTrue(lines[1].startswith("RUN work"), r.stdout)

    def test_status_covers_all_accounts(self):
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("account=personal", r.stdout)
        self.assertIn("account=work", r.stdout)


class TestGateLegacyConfig(unittest.TestCase):
    """A legacy flat config (no sections) keeps working: one synthesized
    account named "default" and one project named "default"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "orchestrator" / "state").mkdir(parents=True)
        (self.root / "tasks").mkdir()
        (self.root / "config.yml").write_text(
            BASE_CFG +
            "weekly_cap_tokens: 100\nwindow_cap_tokens: 15\np90_daily_tokens: 10\n"
            "reset_weekday: 3\nreset_time: 05:59\nreset_tz: Europe/Warsaw\n"
            "extra_dirs: ~/projects\n")
        # A legacy task: no `project:` key at all.
        (self.root / "tasks" / "t1.md").write_text(
            "---\ntitle: X\nstatus: ready\npriority: high\ncreated: 2026-08-01\n---\n")
        fx = self.root / "fixture.json"
        fx.write_text(json.dumps(FIXTURE))
        self.env = {"ORCH_CCUSAGE_JSON": str(fx)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_flat_config_still_runs(self):
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("RUN default 15"), r.stdout + r.stderr)
        self.assertIn("t1.md sonnet low default", r.stdout)


if __name__ == "__main__":
    unittest.main()
