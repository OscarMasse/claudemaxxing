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

# $40 consumed this week, no open 5h window. Budgets are USD at list price.
FIXTURE = {"week_usd": 40, "week_by_family": {}, "block": None,
           "unknown_models": []}

# Shared knobs are flat; calibration lives in the account. reset_weekday 3
# makes Wednesday 2026-08-12 the late-week surplus scenario from the
# controller tests.
BASE_CFG = (
    "dry_run: false\n"
    "night_start: 02:00\nnight_end: 06:00\nmorning_guard: 08:30\n"
    "prereset_burn_hours: 8\nactivity_idle_night_min: 40\n"
    "night_slice_min: 50\n"
    "claude_bin: /usr/local/bin/claude\nclaude_model: sonnet\nclaude_effort: low\n"
)

PERSONAL = (
    "accounts:\n"
    "  - name: personal\n"
    "    claude_config_dir: ~/.claude\n"
    "    weekly_cap_usd: 100\n"
    "    window_cap_usd: 15\n"
    "    p90_daily_usd: 10\n"
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
               ORCH_NOW="2026-08-11T02:30:00+02:00",  # Tuesday night
               ORCH_IDLE_MIN="999",
               ORCH_NO_NOTIFY="1",
               ORCH_DOCKER_BIN="")  # never touch the real docker daemon
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
        self.env = {"ORCH_USAGE_JSON": str(fx)}
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
        """`delivery:` is mandatory in the files, and these tests are about
        scheduling rather than about the delivery contract, so a task written
        without the key gets `branch`. The tests that do exercise the contract
        write the key themselves."""
        if "\ndelivery:" not in text:
            text = text.replace("\nstatus:", "\ndelivery: branch\nstatus:", 1)
        (self.root / "tasks" / name).write_text(text)

    def test_paused_wins(self):
        (self.root / "orchestrator" / "PAUSED").touch()
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP paused"), r.stdout)

    def test_run_decision_and_log(self):
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("RUN personal 50"), r.stdout + r.stderr)
        self.assertIn("t1.md sonnet low side-projects", r.stdout)
        log = (self.root / "orchestrator" / "state" / "gatekeeper.log").read_text()
        self.assertIn("account=personal", log)
        self.assertIn("night regime", log)

    def test_fable_task_picked_at_night(self):
        # Every model the engine knows is reachable at night. The ceiling used
        # to be opus, which made a fable floor schedulable only in the pre-reset
        # burn-down: the tasks that ask for the strongest model waited days for
        # a window they could miss entirely. What a night may spend is the
        # budget's decision, not a second gate on the model name.
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: fable\n---\n")
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("RUN personal 50"), r.stdout)
        self.assertIn("t1.md fable low side-projects", r.stdout)

    def test_daytime_tick_never_runs(self):
        env = dict(self.env, ORCH_NOW="2026-08-12T15:00:00+02:00")
        r = run_gate(self.root, env)
        self.assertTrue(r.stdout.startswith("SKIP personal day:"), r.stdout)

    def test_opus_task_picked_at_night(self):
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: opus\neffort: medium\n---\n")
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("RUN personal 50"), r.stdout)
        self.assertIn("opus medium", r.stdout)

    def test_prereset_runs_each_task_on_its_own_model(self):
        # The burn-down used to upgrade every session it launched to the
        # strongest model the doomed surplus justified, which spends Fable
        # tokens on work that asked for Sonnet. A dying surplus buys more
        # sessions, not dearer ones.
        env = dict(self.env, ORCH_NOW="2026-08-12T23:00:00+02:00")  # <8h to reset
        r = run_gate(self.root, env)
        self.assertTrue(r.stdout.startswith("RUN"), r.stdout + r.stderr)
        self.assertIn("t1.md sonnet", r.stdout)

    def test_prereset_admits_a_fable_task(self):
        # ... and the other half of the same rule: a declared fable floor is
        # schedulable in the burn-down. A ceiling derived from the surplus
        # used to filter these tasks out of the one regime meant to run them.
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: fable\n---\n")
        env = dict(self.env, ORCH_NOW="2026-08-12T23:00:00+02:00")
        r = run_gate(self.root, env)
        self.assertTrue(r.stdout.startswith("RUN"), r.stdout + r.stderr)
        self.assertIn("t1.md fable", r.stdout)

    def test_no_eligible_task(self):
        (self.root / "tasks" / "t1.md").unlink()
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal no eligible task"), r.stdout)

    def test_night_slot_count_is_budget_driven(self):
        # Tuesday night's allocation is $5.5 (see TestNightBudget); at an
        # estimated 2.5 per session, 2 sessions fit and a 3rd does not. The
        # slot ceiling (4) is not what decides this.
        self.append_cfg("max_parallel_sessions: 4\n"
                        "est_session_usd: 2.5\n")
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "parallel: true\n---\n")
        env = dict(self.env, ORCH_NOW="2026-08-11T02:30:00+02:00")
        r = run_gate(self.root, env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len(lines), 2, r.stdout)

    def test_cheap_tasks_fill_more_slots_than_one_budget_would_allow(self):
        # Same night, ten times cheaper: the count rises with the budget, up to
        # the safety ceiling. This is the point of the redesign - the metric is
        # dollars, not a fixed task count.
        self.append_cfg("max_parallel_sessions: 4\n"
                        "est_session_usd: 0.25\n")
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "parallel: true\n---\n")
        env = dict(self.env, ORCH_NOW="2026-08-11T02:30:00+02:00")
        r = run_gate(self.root, env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len(lines), 4, r.stdout)

    def test_safety_ceiling_caps_the_slot_count(self):
        # Budget for many, machine for two: the ceiling wins.
        self.append_cfg("max_parallel_sessions: 2\n"
                        "est_session_usd: 0.25\n")
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "parallel: true\n---\n")
        env = dict(self.env, ORCH_NOW="2026-08-11T02:30:00+02:00")
        r = run_gate(self.root, env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len(lines), 2, r.stdout)

    def test_heavy_task_limits_parallelism(self):
        # One session's estimated burn (50) exceeds the night allocation:
        # parallelizing is pointless, exactly one session runs.
        self.append_cfg("max_parallel_sessions: 4\n"
                        "est_session_usd: 50\n")
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "parallel: true\n---\n")
        env = dict(self.env, ORCH_NOW="2026-08-11T02:30:00+02:00")
        r = run_gate(self.root, env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len(lines), 1, r.stdout)

    def test_run_line_carries_the_launch_estimate(self):
        self.append_cfg("max_parallel_sessions: 1\nest_session_usd: 2.5\n")
        env = dict(self.env, ORCH_NOW="2026-08-11T02:30:00+02:00")
        r = run_gate(self.root, env)
        # "... <est_usd> <delivery>": money, two decimals.
        self.assertTrue(r.stdout.rstrip().endswith(" 2.50 branch"), r.stdout)

    def test_parallel_respects_active_slots(self):
        self.append_cfg("max_parallel_sessions: 2\n"
                        "est_session_usd: 0.25\n")
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

    def test_out_of_quota_model_is_not_launched(self):
        # The account said "You've hit your session limit" for fable at 02:25;
        # the engine must remember that instead of spending its remaining
        # slots relaunching into the same wall (2026-09-09).
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        (state / "exhausted.json").write_text(json.dumps({"fable": {
            "scope": "session", "until": "2026-08-11T04:20:00+02:00",
            "seen": "2026-08-11T02:25:00+02:00"}}))
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: fable\n---\n")
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal no eligible task"),
                        r.stdout)
        log = (self.root / "orchestrator" / "state" / "gatekeeper.log").read_text()
        self.assertIn("out of quota model=fable", log)

    def test_run_line_carries_the_declared_delivery(self):
        # run.sh turns this last field into the session's delivery obligation,
        # so the session is told to push (or not) instead of deciding.
        for value in ("branch", "pr", "local"):
            self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                            f"status: ready\ndelivery: {value}\n"
                            "priority: high\ncreated: 2026-08-01\n---\n")
            r = run_gate(self.root, self.env)
            self.assertTrue(r.stdout.rstrip().endswith(f" {value}"), r.stdout)

    def test_a_task_without_delivery_is_not_launched_but_logged(self):
        # Written directly, bypassing write_task's `delivery: branch` default.
        (self.root / "tasks" / "t1.md").write_text(
            "---\ntitle: X\nproject: side-projects\nstatus: ready\n"
            "priority: high\ncreated: 2026-08-01\n---\n")
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal no eligible task"),
                        r.stdout)
        log = (self.root / "orchestrator" / "state" / "gatekeeper.log").read_text()
        self.assertIn("unschedulable task=t1.md: delivery=<missing>", log)

    def test_status_reports_the_misconfigured_delivery(self):
        (self.root / "tasks" / "t1.md").write_text(
            "---\ntitle: X\nproject: side-projects\nstatus: ready\n"
            "delivery: pull-request\npriority: high\ncreated: 2026-08-01\n---\n")
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("misconfigured task=t1.md delivery=pull-request", r.stdout)

    def test_out_of_quota_model_does_not_block_the_others(self):
        # Fable dead, sonnet fine: the night must keep working. This is what
        # the per-model signal buys over a global backoff.
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        (state / "exhausted.json").write_text(json.dumps({"fable": {
            "scope": "session", "until": "2026-08-11T04:20:00+02:00"}}))
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("RUN personal 50"), r.stdout)
        self.assertIn("t1.md sonnet low", r.stdout)

    def test_expired_exhaustion_record_no_longer_blocks(self):
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        (state / "exhausted.json").write_text(json.dumps({"fable": {
            "scope": "session", "until": "2026-08-11T01:00:00+02:00"}}))
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: fable\n---\n")
        r = run_gate(self.root, self.env)  # tick at 02:30, reset was 01:00
        self.assertIn("t1.md fable low", r.stdout)

    def test_fable_is_serialised_even_when_slots_and_budget_allow_more(self):
        # Fable's binding constraint is its own token limit, not wall-clock
        # time, so parallel fable sessions only race each other to the wall.
        self.append_cfg("max_parallel_sessions: 4\nest_session_usd: 0.25\n"
                        "max_fable_slots: 1\n")
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: fable\nparallel: true\n---\n")
        r = run_gate(self.root, self.env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len(lines), 1, r.stdout)

    def test_the_fable_cap_leaves_the_slots_to_cheaper_models(self):
        # The point of the cap: one fable session, and the slots it does not
        # take go to models that are nowhere near their own limit.
        self.append_cfg("max_parallel_sessions: 4\nest_session_usd: 0.25\n"
                        "max_fable_slots: 1\n")
        self.write_task("t1.md", "---\ntitle: A\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: fable\nparallel: true\n---\n")
        self.write_task("t2.md", "---\ntitle: B\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-02\n"
                        "parallel: true\n---\n")
        r = run_gate(self.root, self.env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len([l for l in lines if " fable " in l]), 1, r.stdout)
        self.assertGreaterEqual(len([l for l in lines if " sonnet " in l]), 1,
                                r.stdout)

    def test_a_running_fable_session_takes_the_fable_slot(self):
        # The cap used to count only this tick's launches: at 11:19 on
        # 2026-09-12 a Fable session was launched next to one from 11:14 that
        # was still running. run.sh writes the model as the lock's third
        # field so the tick can see what the live sessions run on.
        self.append_cfg("max_parallel_sessions: 4\nest_session_usd: 0.25\n"
                        "max_fable_slots: 1\n")
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        (state / "RUNNING.1").write_text(f"999 {int(time.time())} fable")
        for i, name in enumerate(("t1.md", "t2.md")):
            self.write_task(name, f"---\ntitle: {name}\nproject: side-projects\n"
                            f"status: ready\npriority: high\ncreated: 2026-08-0{i+1}\n"
                            "model: fable\n---\n")
        self.write_task("t3.md", "---\ntitle: C\nproject: side-projects\n"
                        "status: ready\npriority: low\ncreated: 2026-08-03\n---\n")
        r = run_gate(self.root, self.env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len([l for l in lines if " fable " in l]), 0, r.stdout)
        self.assertEqual(len([l for l in lines if " sonnet " in l]), 1, r.stdout)

    def test_a_lock_without_a_model_field_still_counts_as_a_slot(self):
        # Digest runs and pre-change locks carry no model: they hold a slot
        # but no model slot.
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        (state / "RUNNING.1").write_text(f"999 {int(time.time())}")
        (state / "RUNNING.2").write_text(f"999 {int(time.time())} fable")
        p = gate.paths()
        self.assertEqual(gate.active_slots(p, state), 2)
        self.assertEqual(gate.running_models(state), ["fable"])

    def test_a_fable_heavy_queue_head_does_not_leave_slots_empty(self):
        # 2026-09-12, 11:00: candidates opus, fable, fable, sonnet with 4 free
        # slots and one fable slot launched only 2. The model filter has to
        # run before the queue is cut to the slot count.
        self.append_cfg("max_parallel_sessions: 4\nest_session_usd: 0.25\n"
                        "max_fable_slots: 1\n")
        (self.root / "tasks" / "t1.md").unlink()
        for i, model in enumerate(("opus", "fable", "fable", "sonnet", "sonnet")):
            self.write_task(f"q{i}.md", f"---\ntitle: {i}\nproject: side-projects\n"
                            f"status: ready\npriority: high\ncreated: 2026-08-0{i+1}\n"
                            f"model: {model}\n---\n")
        r = run_gate(self.root, self.env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual([l.split()[4] for l in lines],
                         ["opus", "fable", "sonnet", "sonnet"], r.stdout)

    def test_an_out_of_quota_family_does_not_eat_a_slot(self):
        self.append_cfg("max_parallel_sessions: 2\nest_session_usd: 0.25\n")
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        (state / "exhausted.json").write_text(json.dumps({"opus": {
            "until": "2026-08-11T04:20:00+02:00", "scope": "session"}}))
        (self.root / "tasks" / "t1.md").unlink()
        for i, model in enumerate(("opus", "opus", "sonnet", "sonnet")):
            self.write_task(f"q{i}.md", f"---\ntitle: {i}\nproject: side-projects\n"
                            f"status: ready\npriority: high\ncreated: 2026-08-0{i+1}\n"
                            f"model: {model}\n---\n")
        r = run_gate(self.root, self.env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual([l.split()[4] for l in lines], ["sonnet", "sonnet"],
                         r.stdout)

    def test_fable_cap_is_configurable(self):
        self.append_cfg("max_parallel_sessions: 4\nest_session_usd: 0.25\n"
                        "max_fable_slots: 2\n")
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "model: fable\nparallel: true\n---\n")
        r = run_gate(self.root, self.env)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RUN")]
        self.assertEqual(len(lines), 2, r.stdout)

    def test_launch_claims_the_task(self):
        # The session used to be the one setting `in-progress`, minutes after
        # launch; on 2026-09-12 the next tick still saw the task `ready` and
        # launched a twin into the same worktree (4.7M tokens).
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("RUN personal 50"), r.stdout)
        text = (self.root / "tasks" / "t1.md").read_text()
        self.assertIn("status: in-progress", text)
        self.assertNotIn("status: ready", text)
        self.assertIn("claimed by the gatekeeper at launch (sonnet, slice 50 min)",
                      text)
        # Same clock, one tick later: the claim is what keeps it off the queue.
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal no eligible task"),
                        r.stdout)

    def test_dry_run_does_not_claim(self):
        cfg = self.root / "config.yml"
        cfg.write_text(cfg.read_text().replace("dry_run: false", "dry_run: true"))
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal dry_run"), r.stdout)
        self.assertIn("status: ready", (self.root / "tasks" / "t1.md").read_text())

    def test_a_duty_is_launched_but_never_claimed(self):
        # Duties stay `ready` by contract; duties.json is what stops a relaunch.
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "duty: nightly\n---\n")
        r = run_gate(self.root, self.env)
        self.assertIn("t1.md", r.stdout)
        self.assertIn("status: ready", (self.root / "tasks" / "t1.md").read_text())

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
                   ORCH_NOW="2026-08-11T02:30:00+02:00",
                   ORCH_IDLE_MIN="999",
                   ORCH_NO_NOTIFY="1",
                   ORCH_DOCKER_BIN="")
        env.pop("ORCH_ROOT", None)
        env.pop("ORCH_CONFIG", None)
        env.update(self.env)
        r = subprocess.run(["python3", str(ORCH / "gate.py"), "tick"],
                           capture_output=True, text=True, env=env, cwd=ORCH)
        self.assertTrue(r.stdout.startswith("RUN personal 50"),
                        r.stdout + r.stderr)
        self.assertIn(str(self.root / "tasks" / "t1.md"), r.stdout)
        log = self.root / "orchestrator" / "state" / "gatekeeper.log"
        self.assertTrue(log.exists())
        self.assertIn("account=personal", log.read_text())

    def test_stale_lock_broken_fresh_lock_respected(self):
        self.append_cfg("max_parallel_sessions: 1\n")
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
        self.assertIn("week_usd=40.00", r.stdout)
        self.assertIn("cap=100.00", r.stdout)
        self.assertIn("available", r.stdout)

    def test_status_prints_the_two_usage_percentages(self):
        # The digest puts these next to the real /usage bars so the owner can
        # correct the caps when the calibration drifts. No open window: none.
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("usage_week_pct=40.0", r.stdout)
        self.assertIn("usage_window_pct=none", r.stdout)
        fx = self.root / "window.json"
        fx.write_text(json.dumps({
            "week_usd": 40, "week_by_family": {},
            "block": {"start": "2026-08-11T00:00:00+02:00",
                      "end": "2026-08-11T05:00:00+02:00", "usd": 3.0,
                      "by_family": {"sonnet": 3.0}}}))
        r = run_gate(self.root, {"ORCH_USAGE_JSON": str(fx)}, arg="status")
        self.assertIn("usage_window_pct=20.0", r.stdout)  # 3 / 15

    def test_a_retired_token_key_makes_the_account_misconfigured(self):
        # No conversion factor exists between the old unit and dollars, so a
        # leftover *_tokens key is refused, not converted, in tick and status.
        self.append_cfg("est_session_tokens: 2500000\n")
        r = run_gate(self.root, self.env)
        expected = ("account=personal misconfigured: retired key "
                    "est_session_tokens, the unit is USD since 2026-09-12 "
                    "(see specs)")
        self.assertTrue(r.stdout.startswith("SKIP personal misconfigured"), r.stdout)
        log = (self.root / "orchestrator" / "state" / "gatekeeper.log").read_text()
        self.assertIn(expected, log)
        self.assertIn("status: ready", (self.root / "tasks" / "t1.md").read_text())
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn(expected, r.stdout)
        self.assertNotIn("week_usd", r.stdout)

    def test_a_missing_usd_cap_is_an_error_not_a_default(self):
        self.write_cfg(BASE_CFG + PERSONAL.replace("    weekly_cap_usd: 100\n", "")
                       + PROJECTS)
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith(
            "SKIP personal misconfigured: missing key weekly_cap_usd"), r.stdout)

    def test_status_relays_the_promo_state_and_the_per_model_split(self):
        self.write_cfg(BASE_CFG + PERSONAL
                       + "    promo_multiplier: 1.5\n"
                         "    promo_until: 2026-08-30\n" + PROJECTS)
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("promo active until 2026-08-30", r.stdout)

    def test_status_warns_before_the_promo_ends(self):
        self.write_cfg(BASE_CFG + PERSONAL
                       + "    promo_multiplier: 1.5\n"
                         "    promo_until: 2026-08-12\n" + PROJECTS)
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("promo ENDS 2026-08-12 (in 1d)", r.stdout)
        self.assertIn("NEEDS-HUMAN", r.stdout)

    def test_status_keeps_asking_after_the_promo_expired(self):
        # The cap can only be re-read off /usage by a human, so this line does
        # not go away on its own: it repeats until promo_until is updated.
        self.write_cfg(BASE_CFG + PERSONAL
                       + "    promo_multiplier: 1.5\n"
                         "    promo_until: 2026-08-01\n" + PROJECTS)
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("promo EXPIRED 2026-08-01 (10d ago)", r.stdout)
        self.assertIn("NEEDS-HUMAN", r.stdout)

    def test_status_reports_per_model_usage_and_unknown_ids(self):
        # An id the engine cannot classify still spends the owner's quota, so
        # it is counted and reported rather than silently free.
        fx = self.root / "snapshot.json"
        fx.write_text(json.dumps({
            "week_usd": 30,
            "week_by_family": {"fable": 20, "sonnet": 10},
            "block": None,
            "unknown_models": ["claude-something-new"]}))
        r = run_gate(self.root, {"ORCH_USAGE_JSON": str(fx)}, arg="status")
        self.assertIn("week_model=fable usd=20.00", r.stdout)
        self.assertIn("week_model=sonnet usd=10.00", r.stdout)
        self.assertIn("unknown_model id=claude-something-new", r.stdout)

    def test_status_reports_an_exhausted_model(self):
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        (state / "exhausted.json").write_text(json.dumps({"fable": {
            "scope": "session", "until": "2026-08-11T04:20:00+02:00"}}))
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("out_of_quota model=fable until=2026-08-11T04:20:00+02:00",
                      r.stdout)

    def test_status_shows_blocked_task_with_unmet_prerequisites(self):
        self.write_task("t1.md", "---\ntitle: X\nproject: side-projects\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n"
                        "prerequisites: dep\n---\n")
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("blocked task=t1.md unmet=dep", r.stdout)

    def test_status_shows_orphaned_task(self):
        self.write_task("t2.md", "---\ntitle: X\nproject: typo\n"
                        "status: ready\npriority: high\ncreated: 2026-08-01\n---\n")
        r = run_gate(self.root, self.env, arg="status")
        self.assertIn("orphaned task=t2.md project=typo", r.stdout)

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


class TestGateJanitor(unittest.TestCase):
    """When a tick tears down agent-worktree stacks (lib/janitor.py): only at
    night, with no session running and the owner away. A stub docker CLI
    lists one agent stack under the project dir and records `compose` calls.
    Borrows TestGate's fixture without inheriting its tests."""
    tearDown = TestGate.tearDown
    write_cfg = TestGate.write_cfg
    write_task = TestGate.write_task

    def setUp(self):
        TestGate.setUp(self)
        proj = self.root / "proj"
        self.write_cfg(BASE_CFG + PERSONAL
                       + PROJECTS.replace("~/projects", str(proj)))
        self.calls = self.root / "docker-calls"
        stub = self.root / "docker"
        stub.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = ps ]; then "
            f"printf 'proj-a\\t{proj}/.agent-worktrees/proj-a\\n'; exit 0; fi\n"
            f"echo \"$@\" >> {self.calls}\n")
        stub.chmod(0o755)
        self.env = dict(self.env, ORCH_DOCKER_BIN=str(stub))

    def downs(self):
        return self.calls.read_text() if self.calls.exists() else ""

    def test_night_idle_nothing_running_reaps(self):
        run_gate(self.root, self.env)
        self.assertEqual(self.downs(), "compose -p proj-a down --remove-orphans\n")
        log = (self.root / "orchestrator" / "state" / "gatekeeper.log").read_text()
        self.assertIn("janitor compose down project=proj-a ok=True", log)

    def test_running_session_protects_stacks(self):
        state = self.root / "orchestrator" / "state" / "personal"
        state.mkdir(parents=True)
        (state / "RUNNING.1").write_text(f"999 {int(time.time())}")
        run_gate(self.root, self.env)
        self.assertEqual(self.downs(), "")

    def test_daytime_never_reaps(self):
        run_gate(self.root, dict(self.env, ORCH_NOW="2026-08-11T14:00:00+02:00"))
        self.assertEqual(self.downs(), "")

    def test_owner_present_never_reaps(self):
        run_gate(self.root, dict(self.env, ORCH_IDLE_MIN="2"))
        self.assertEqual(self.downs(), "")


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


class TestNotifyHookCall(unittest.TestCase):
    """What the platform notify hook is handed, so the alert is actionable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "orchestrator" / "state").mkdir(parents=True)
        self.needs = root / "NEEDS-HUMAN.md"
        self.needs.write_text("# Needs human\n\n- [ ] task-x: which color?\n")
        self.p = {"state": root / "orchestrator" / "state",
                  "needs": self.needs,
                  "notified": root / "orchestrator" / "state" / "notified.txt"}

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, open_cmd):
        with mock.patch.dict(os.environ, {"ORCH_PLATFORM": "macos"}), \
             mock.patch.object(gate.subprocess, "run") as run:
            gate.notify_duty(self.p, 1, open_cmd)
        self.assertEqual(run.call_count, 1)
        return run.call_args

    def test_hook_receives_the_file_to_open(self):
        # A question is a checkbox to answer and tick; a notification that
        # cannot take the owner to it makes them hunt for the file.
        args, kwargs = self.call("zed")
        self.assertEqual(args[0][3], str(self.needs))
        self.assertEqual(kwargs["env"]["ORCH_NOTIFY_OPEN"], "zed")

    def test_no_open_command_configured_leaves_the_default_to_the_hook(self):
        _args, kwargs = self.call(None)
        self.assertNotIn("ORCH_NOTIFY_OPEN", kwargs["env"])


class TestGateMultiAccount(unittest.TestCase):
    """Two accounts scheduled in the same tick, fully isolated."""

    WORK = (
        "  - name: work\n"
        "    claude_config_dir: ~/.claude-work\n"
        "    weekly_cap_usd: 100\n"
        "    window_cap_usd: 15\n"
        "    p90_daily_usd: 10\n"
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
            "delivery: branch\npriority: high\ncreated: 2026-08-01\n---\n")
        (self.root / "tasks" / "w.md").write_text(
            "---\ntitle: W\nproject: job\nstatus: ready\n"
            "delivery: branch\npriority: high\ncreated: 2026-08-01\n---\n")
        fx = self.root / "fixture.json"
        fx.write_text(json.dumps(FIXTURE))
        self.env = {"ORCH_USAGE_JSON": str(fx)}

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self, r):
        return [l for l in r.stdout.splitlines() if l]

    def test_both_accounts_run_in_one_tick(self):
        r = run_gate(self.root, self.env)
        lines = self.lines(r)
        self.assertEqual(len(lines), 2, r.stdout + r.stderr)
        self.assertIn("RUN personal 50", lines[0])
        self.assertIn("p.md sonnet low side-projects", lines[0])
        self.assertIn("RUN work 50", lines[1])
        self.assertIn("w.md sonnet low job", lines[1])

    def test_budget_isolation(self):
        # The work account's week is exhausted; personal still runs.
        heavy = self.root / "heavy.json"
        heavy.write_text(json.dumps(dict(FIXTURE, week_usd=999)))
        env = dict(self.env, ORCH_USAGE_JSON_WORK=str(heavy))
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
        cfg = self.root / "config.yml"
        cfg.write_text(cfg.read_text() + "max_parallel_sessions: 1\n")
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

    def test_a_misconfigured_account_does_not_stop_the_other(self):
        # One subscription left in the token unit; the other keeps its night.
        cfg = self.root / "config.yml"
        cfg.write_text(cfg.read_text().replace(
            "    claude_config_dir: ~/.claude-work\n",
            "    claude_config_dir: ~/.claude-work\n"
            "    fable_min_surplus_tokens: 100000000\n"))
        r = run_gate(self.root, self.env)
        lines = self.lines(r)
        self.assertTrue(lines[0].startswith("RUN personal"), r.stdout + r.stderr)
        self.assertEqual(lines[1], "SKIP work misconfigured: retired key "
                                   "fable_min_surplus_tokens, the unit is USD "
                                   "since 2026-09-12 (see specs)", r.stdout)
        log = (self.root / "orchestrator" / "state" / "gatekeeper.log").read_text()
        self.assertIn("account=work misconfigured: retired key "
                      "fable_min_surplus_tokens", log)
        self.assertIn("status: ready", (self.root / "tasks" / "w.md").read_text())


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
            "weekly_cap_usd: 100\nwindow_cap_usd: 15\np90_daily_usd: 10\n"
            "reset_weekday: 3\nreset_time: 05:59\nreset_tz: Europe/Warsaw\n"
            "extra_dirs: ~/projects\n")
        # A legacy task: no `project:` key at all.
        (self.root / "tasks" / "t1.md").write_text(
            "---\ntitle: X\nstatus: ready\ndelivery: branch\n"
            "priority: high\ncreated: 2026-08-01\n---\n")
        fx = self.root / "fixture.json"
        fx.write_text(json.dumps(FIXTURE))
        self.env = {"ORCH_USAGE_JSON": str(fx)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_flat_config_still_runs(self):
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("RUN default 50"), r.stdout + r.stderr)
        self.assertIn("t1.md sonnet low default", r.stdout)


class TestGateDuties(unittest.TestCase):
    """Recurring classes at the gate: mandatory duties, surplus-only fillers."""

    NIGHT = "2026-08-11T02:30:00+02:00"  # Tuesday night, allocation $5.5

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "orchestrator" / "state").mkdir(parents=True)
        (self.root / "tasks").mkdir()
        (self.root / "fixture.json").write_text(json.dumps(FIXTURE))
        self.env = {"ORCH_USAGE_JSON": str(self.root / "fixture.json"),
                    "ORCH_NOW": self.NIGHT}
        (self.root / "config.yml").write_text(
            BASE_CFG + "max_parallel_sessions: 4\n"
            + PERSONAL + PROJECTS)

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, name, extra=""):
        (self.root / "tasks" / name).write_text(
            "---\ntitle: X\nproject: side-projects\nstatus: ready\n"
            "delivery: branch\npriority: high\ncreated: 2026-08-01\n"
            + extra + "---\n")

    def cost(self, per_session):
        cfg = self.root / "config.yml"
        cfg.write_text(cfg.read_text() + f"est_session_usd: {per_session}\n")

    def runs(self, r):
        return [l for l in r.stdout.splitlines() if l.startswith("RUN")]

    def served(self):
        f = self.root / "orchestrator" / "state" / "personal" / "duties.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def test_duty_runs_even_when_the_budget_is_gone(self):
        # Each session is estimated at 50, far over the 8.6 allocation. The queue task is squeezed out; the duty is not.
        self.cost(50)
        self.task("queue.md")
        self.task("sync.md", "duty: nightly\n")
        lines = self.runs(run_gate(self.root, self.env))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("sync.md", lines[0])

    def test_duty_launch_is_recorded_once_per_night(self):
        self.cost(2.5)
        self.task("sync.md", "duty: nightly\n")
        self.assertEqual(len(self.runs(run_gate(self.root, self.env))), 1)
        self.assertEqual(list(self.served().values()), ["2026-08-11"])
        # A second tick the same night must not relaunch it.
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal no eligible task"),
                        r.stdout)

    def test_duty_is_due_again_the_next_night(self):
        self.cost(2.5)
        self.task("sync.md", "duty: nightly\n")
        run_gate(self.root, self.env)
        env = dict(self.env, ORCH_NOW="2026-08-12T02:30:00+02:00")
        self.assertEqual(len(self.runs(run_gate(self.root, env))), 1)

    def test_dry_run_does_not_consume_the_duty_period(self):
        cfg = self.root / "config.yml"
        cfg.write_text(cfg.read_text().replace("dry_run: false", "dry_run: true"))
        self.cost(2.5)
        self.task("sync.md", "duty: nightly\n")
        r = run_gate(self.root, self.env)
        self.assertTrue(r.stdout.startswith("SKIP personal dry_run"), r.stdout)
        self.assertEqual(self.served(), {})

    def test_nightly_duty_is_not_launched_during_the_day(self):
        # Belt and braces: the daytime tick skips before selection anyway, but
        # the period key is also absent, so nothing can pick the duty up.
        self.cost(2.5)
        self.task("sync.md", "duty: nightly\n")
        day = gate.now_from_env.__globals__["datetime"].fromisoformat(
            "2026-08-12T15:00:00+02:00")
        acct = {"reset_tz": "Europe/Warsaw", "reset_weekday": 3,
                "reset_time": "05:59", "night_start": "02:00", "night_end": "06:00"}
        self.assertNotIn("nightly", gate.period_keys(acct, day))
        r = run_gate(self.root, dict(self.env, ORCH_NOW="2026-08-12T15:00:00+02:00"))
        self.assertTrue(r.stdout.startswith("SKIP personal day:"), r.stdout)

    def test_filler_runs_on_leftover_budget(self):
        self.cost(2.5)
        self.task("queue.md")
        self.task("tidy.md", "filler: true\n")
        lines = self.runs(run_gate(self.root, self.env))
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("queue.md", lines[0])
        self.assertIn("tidy.md", lines[1])

    def test_filler_never_runs_on_borrowed_budget(self):
        # The queue task alone overshoots the allocation: unlike a queue task,
        # a filler never gets the "first session always runs" exemption.
        self.cost(50)
        self.task("queue.md")
        self.task("tidy.md", "filler: true\n")
        lines = self.runs(run_gate(self.root, self.env))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("queue.md", lines[0])

    def test_status_reports_duties_and_fillers(self):
        self.task("sync.md", "duty: nightly\n")
        self.task("daily.md", "duty: daily\n")
        self.task("tidy.md", "filler: true\n")
        out = run_gate(self.root, self.env, arg="status").stdout
        self.assertIn("duty task=sync.md period=nightly supported=yes due=yes", out)
        self.assertIn("duty task=daily.md period=daily supported=NO due=no", out)
        self.assertIn("filler task=tidy.md", out)


if __name__ == "__main__":
    unittest.main()
