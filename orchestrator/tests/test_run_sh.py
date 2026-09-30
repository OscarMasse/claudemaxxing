"""run.sh end to end, with a fake `claude` binary standing in for the session."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ORCH = Path(__file__).resolve().parents[1]

CFG = (
    "night_start: 02:00\nnight_end: 06:00\nmorning_guard: 08:30\n"
    "prereset_burn_hours: 8\nactivity_idle_night_min: 40\n"
    "night_slice_min: 50\nmax_parallel_sessions: 2\n"
    "claude_bin: {bin}\nclaude_model: sonnet\nclaude_effort: low\n"
    "accounts:\n"
    "  - name: personal\n"
    "    claude_config_dir: {root}/profile\n"
    "    reset_weekday: 3\n"
    "    reset_time: 05:59\n"
    "    reset_tz: Europe/Warsaw\n"
    "projects:\n"
    "  - name: side-projects\n"
    "    account: personal\n"
    "    dirs: {root}/projects\n"
    "    rank: 1\n"
)

# Saves the prompt it receives on stdin, then answers like `claude -p
# --output-format stream-json --verbose` would after running FAKE_DURATION_MS.
FAKE_CLAUDE = """#!/bin/bash
cat > "$FAKE_PROMPT_OUT"
printf '%s\\n' "$@" > "$FAKE_ARGS_OUT"
pwd -P > "$FAKE_ARGS_OUT.cwd"
printf '{"type": "system", "subtype": "init"}\n'
printf '{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.25, "resetsAt": 4102444800}, "seven_day": {"utilization": 0.5, "resetsAt": 4102444800}}}}\n'
printf '{"type": "result", "total_cost_usd": 0.5, "duration_ms": %s, "result": "ok"}\n' "$FAKE_DURATION_MS"
"""


class RunShTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()
        (self.root / "projects").mkdir()
        fake = self.root / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        cfg = self.root / "config.yaml"
        cfg.write_text(CFG.format(bin=fake, root=self.root))
        self.task = self.root / "tasks" / "t.md"
        self.task.write_text("---\ntitle: T\nproject: side-projects\n"
                             "status: in-progress\ndelivery: local\n---\n\n## Notes\n")
        self.env = dict(os.environ, BACKLOG_ROOT=str(self.root),
                        ORCH_CONFIG=str(cfg), ORCH_PLATFORM="none",
                        HOME=str(self.root),
                        FAKE_PROMPT_OUT=str(self.root / "prompt.txt"),
                        FAKE_ARGS_OUT=str(self.root / "args.txt"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_session(self, duration_min, end_status):
        """One session that runs `duration_min` and leaves the task `end_status`."""
        self.task.write_text(self.task.read_text().replace(
            "status: in-progress", f"status: {end_status}"))
        env = dict(self.env, FAKE_DURATION_MS=str(duration_min * 60000))
        r = subprocess.run(
            ["bash", str(ORCH / "run.sh"), "--account", "personal", "50",
             str(self.task), "sonnet", "low", "side-projects", "0", "local"],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        runs = (self.root / "orchestrator" / "state" / "runs.log").read_text()
        digest = next((self.root / "digests").glob("*.md")).read_text()
        return runs.splitlines()[-1], digest.splitlines()[-1]

    def add_dirs(self):
        args = (self.root / "args.txt").read_text().splitlines()
        return [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]

    def test_repo_task_gets_its_worktree_never_the_main_checkout(self):
        from tests.test_workspace import make_repo
        # Resolved: macOS temp dirs sit behind the /var -> /private/var link,
        # and the worktree path comes back resolved.
        repo = make_repo(self.root.resolve(), "webapp")
        cfg = Path(self.env["ORCH_CONFIG"])
        cfg.write_text(cfg.read_text().replace(
            f"dirs: {self.root}/projects", f"dirs: {repo}"))
        self.run_session(40, "ready")
        wt = repo / ".agent-worktrees" / "t"
        self.assertIn(str(wt), self.add_dirs())
        self.assertNotIn(str(repo), self.add_dirs())
        self.assertIn(str(wt), (self.root / "prompt.txt").read_text())
        # Started outside every repo, not in the engine's own checkout.
        cwd = (self.root / "args.txt.cwd").read_text().strip()
        self.assertEqual(cwd, str((self.root / ".local/state/claudemaxxing/session").resolve()))
        self.assertEqual(
            subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                           capture_output=True, text=True).stdout.strip(),
            "agent/t")

    def optional_assets(self):
        from tests.test_workspace import make_repo
        root = self.root.resolve()
        repo, assets = make_repo(root, "webapp"), make_repo(root, "big-assets")
        cfg = Path(self.env["ORCH_CONFIG"])
        cfg.write_text(cfg.read_text().replace(
            f"dirs: {self.root}/projects",
            f"dirs: {repo}\n    optional_dirs: [{assets}]"))
        return assets

    def disallowed(self):
        args = (self.root / "args.txt").read_text().splitlines()
        rest = args[args.index("--disallowedTools") + 1:]
        end = next((i for i, a in enumerate(rest) if a.startswith("--")), len(rest))
        return rest[:end]

    def test_undeclared_optional_dir_is_readonly_main_checkout(self):
        assets = self.optional_assets()
        self.run_session(40, "ready")
        self.assertIn(str(assets), self.add_dirs())
        self.assertFalse((assets / ".agent-worktrees").exists())
        self.assertIn(f"Write({assets}/**)", self.disallowed())
        self.assertIn(f"Bash(git -C {assets} commit*)", self.disallowed())

    def test_declared_optional_dir_gets_a_worktree_and_no_deny(self):
        assets = self.optional_assets()
        self.task.write_text(self.task.read_text().replace(
            "delivery: local", "delivery: local\nuses: [big-assets]"))
        self.run_session(40, "ready")
        wt = assets / ".agent-worktrees" / "t"
        self.assertIn(str(wt), self.add_dirs())
        self.assertNotIn(str(assets), self.add_dirs())
        self.assertFalse(any(str(assets) in r for r in self.disallowed()))

    def test_session_is_not_launched_without_its_workspace(self):
        self.task.write_text(self.task.read_text().replace(
            "delivery: local", "delivery: local\nworkdir: checkout"))
        r = subprocess.run(
            ["bash", str(ORCH / "run.sh"), "--account", "personal", "50",
             str(self.task), "sonnet", "low", "side-projects", "0", "local"],
            env=dict(self.env, FAKE_DURATION_MS="1"),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.root / "prompt.txt").exists())
        runs = (self.root / "orchestrator" / "state" / "runs.log").read_text()
        self.assertIn("task=t.md workspace failed, not launching", runs)

    def test_prompt_carries_the_slice_start_and_deadline(self):
        self.run_session(40, "ready")
        prompt = (self.root / "prompt.txt").read_text()
        self.assertRegex(prompt, r"started at \d{4}-\d\d-\d\d \d\d:\d\d \S+")
        self.assertRegex(prompt, r"ends at \d{4}-\d\d-\d\d \d\d:\d\d \S+")
        self.assertIn("run `date`", prompt)
        self.assertNotIn("{{", prompt)

    def test_stream_json_feeds_ledger_and_headless_reading(self):
        self.run_session(50, "ready")
        args = (self.root / "args.txt").read_text().splitlines()
        i = args.index("--output-format")
        self.assertEqual(args[i + 1:i + 3], ["stream-json", "--verbose"])
        state = self.root / "orchestrator" / "state" / "personal"
        cost = json.loads((state / "costs.jsonl").read_text().splitlines()[-1])
        self.assertEqual((cost["cost_usd"], cost["exit"]), (0.5, 0))
        row = json.loads((state / "rate_limits.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["origin"], "headless")
        self.assertEqual(row["five_hour"]["used_percentage"], 25.0)
        self.assertEqual(row["seven_day"]["used_percentage"], 50.0)

    def test_early_exit_with_work_left_is_tagged(self):
        run, digest = self.run_session(3, "ready")
        self.assertRegex(run, r"slice=50min .* early_exit exit=0$")
        self.assertIn("3min of 50", digest)
        self.assertIn("early exit", digest)

    def test_session_that_used_its_slice_is_not_tagged(self):
        run, digest = self.run_session(40, "ready")
        self.assertNotIn("early_exit", run)
        self.assertNotIn("early exit", digest)

    def test_task_finished_early_is_not_tagged(self):
        run, _ = self.run_session(3, "done")
        self.assertNotIn("early_exit", run)


class PromptTest(unittest.TestCase):
    def test_every_template_placeholder_is_filled_by_run_sh(self):
        import re
        passed = set(re.findall(r'"([A-Z_]+)=\$', (ORCH / "run.sh").read_text()))
        for tpl in (ORCH / "prompts").glob("*.md"):
            used = set(re.findall(r"\{\{([A-Z_]+)\}\}", tpl.read_text()))
            self.assertLessEqual(used, passed, tpl.name)

    def test_render_refuses_an_unfilled_placeholder(self):
        from lib import prompt
        with self.assertRaises(KeyError):
            prompt.render("{{A}} {{B}}", {"A": "x"})
        self.assertEqual(prompt.render("{{A}}|{{A}}", {"A": "a|b&c"}), "a|b&c|a|b&c")


if __name__ == "__main__":
    unittest.main()
