import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "token_budget.py"


def assistant(msg_id, context, output):
    return {"type": "assistant", "message": {"id": msg_id, "usage": {
        "input_tokens": context, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": output}}}


class TestTokenBudgetHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.transcript = self.dir / "t.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *records, raw=""):
        self.transcript.write_text(
            "".join(json.dumps(r) + "\n" for r in records) + raw)

    def run_hook(self, budget="10k", stdin=None, session="s1"):
        env = {k: v for k, v in os.environ.items()
               if k != "ORCH_TOKEN_BUDGET"}
        env["ORCH_HOOK_STATE_DIR"] = str(self.dir)
        if budget is not None:
            env["ORCH_TOKEN_BUDGET"] = budget
        if stdin is None:
            stdin = json.dumps({"session_id": session,
                                "hook_event_name": "PreToolUse",
                                "transcript_path": str(self.transcript)})
        p = subprocess.run([sys.executable, str(HOOK)], input=stdin,
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def assert_warning(self, out, percent):
        ctx = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PreToolUse")
        self.assertIn(f"{percent}%", ctx["additionalContext"])
        self.assertIn("## Notes", ctx["additionalContext"])

    def test_50_percent_is_silent(self):
        self.write(assistant("m1", 4000, 1000))
        self.assertEqual(self.run_hook(), "")

    def test_85_percent_warns_once(self):
        self.write(assistant("m1", 8000, 500))
        self.assert_warning(self.run_hook(), 85)
        self.assertEqual(self.run_hook(), "")

    def test_140_percent_warns_once_not_twice(self):
        self.write(assistant("m1", 13000, 1000))
        out = self.run_hook()
        self.assertEqual(out.count("hookSpecificOutput"), 1)
        self.assert_warning(out, 140)
        self.assertEqual(self.run_hook(), "")

    def test_rewarns_on_further_growth(self):
        self.write(assistant("m1", 8500, 0))
        self.assert_warning(self.run_hook(), 85)
        self.write(assistant("m1", 8500, 0), assistant("m2", 9000, 0))
        self.assertEqual(self.run_hook(), "")  # same 80-100% bucket
        self.write(assistant("m2", 10500, 0))
        self.assert_warning(self.run_hook(), 105)

    def test_buckets_are_per_session(self):
        self.write(assistant("m1", 8500, 0))
        self.assert_warning(self.run_hook(session="a"), 85)
        self.assert_warning(self.run_hook(session="b"), 85)

    def test_uses_latest_context_and_output_counted_once(self):
        # Records split from one API message share its id and usage.
        self.write(assistant("m1", 1000, 3000), assistant("m1", 1000, 3000),
                   {"type": "user", "message": {"content": "x"}},
                   assistant("m2", 2000, 0))
        self.assertEqual(self.run_hook(), "")  # 2000 + 3000 = 50%
        self.write(assistant("m1", 1000, 3000), assistant("m2", 6000, 0))
        self.assert_warning(self.run_hook(), 90)

    def test_budget_units(self):
        self.write(assistant("m1", 1_700_000, 0))
        self.assert_warning(self.run_hook(budget="2m"), 85)
        self.assert_warning(self.run_hook(budget="1000000", session="s2"),
                            170)

    def test_malformed_inputs_are_silent_and_exit_zero(self):
        self.write(assistant("m1", 9000, 0), raw="{not json\n[1,2]\n\"s\"\n")
        for budget in (None, "", "lots", "0", "-5k"):
            self.assertEqual(self.run_hook(budget=budget), "")
        for stdin in ("", "not json", "[]", json.dumps({"session_id": "x"}),
                      json.dumps({"transcript_path": str(self.dir / "no")})):
            self.assertEqual(self.run_hook(stdin=stdin), "")
        self.transcript.write_text("garbage\n")
        self.assertEqual(self.run_hook(), "")

    def test_tolerates_garbage_lines_among_valid_ones(self):
        self.write(assistant("m1", 9000, 0), raw="{broken\n")
        self.assert_warning(self.run_hook(), 90)


class TestSettingsFile(unittest.TestCase):
    def test_registers_the_hook(self):
        path = HOOK.parent.parent / "session-settings.json"
        hooks = json.loads(path.read_text())["hooks"]["PreToolUse"]
        self.assertIn("hooks/token_budget.py", hooks[0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
