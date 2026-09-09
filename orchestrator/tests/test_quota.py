import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ORCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH))
from lib import quota  # noqa: E402

WARSAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 9, 9, 2, 25, tzinfo=WARSAW)

SESSION_MSG = ("Error: You've hit your session limit · "
               "resets 4:20am (Europe/Warsaw)")
WEEKLY_MSG = ("Error: You've hit your weekly limit · "
              "resets Sep 11 at 6am (Europe/Warsaw)")


class TestParse(unittest.TestCase):
    def test_session_limit(self):
        scope, reset = quota.parse(SESSION_MSG, NOW)
        self.assertEqual(scope, "session")
        self.assertEqual(reset, datetime(2026, 9, 9, 4, 20, tzinfo=WARSAW))

    def test_weekly_limit_with_a_date(self):
        # The weekly wording qualifies the reset with a month and day; without
        # it the parser would anchor to the next 6am and unblock four days
        # early.
        scope, reset = quota.parse(WEEKLY_MSG, NOW)
        self.assertEqual(scope, "weekly")
        self.assertEqual(reset, datetime(2026, 9, 11, 6, 0, tzinfo=WARSAW))

    def test_time_already_past_today_rolls_to_tomorrow(self):
        _scope, reset = quota.parse(
            "You've hit your session limit · resets 1am", NOW,
            "Europe/Warsaw")
        self.assertEqual(reset, datetime(2026, 9, 10, 1, 0, tzinfo=WARSAW))

    def test_pm_and_bare_hour(self):
        _scope, reset = quota.parse(
            "You've hit your session limit · resets 11pm", NOW,
            "Europe/Warsaw")
        self.assertEqual(reset, datetime(2026, 9, 9, 23, 0, tzinfo=WARSAW))

    def test_midnight(self):
        _scope, reset = quota.parse(
            "You've hit your session limit · resets 12am", NOW,
            "Europe/Warsaw")
        self.assertEqual(reset, datetime(2026, 9, 10, 0, 0, tzinfo=WARSAW))

    def test_date_in_the_past_rolls_the_year(self):
        end_of_year = datetime(2026, 12, 30, 23, 0, tzinfo=WARSAW)
        _scope, reset = quota.parse(
            "You've hit your weekly limit · resets Jan 2 at 6am "
            "(Europe/Warsaw)", end_of_year)
        self.assertEqual(reset, datetime(2027, 1, 2, 6, 0, tzinfo=WARSAW))

    def test_default_tz_when_none_is_stated(self):
        _scope, reset = quota.parse(
            "You've hit your session limit · resets 4am", NOW,
            "Europe/Warsaw")
        self.assertEqual(reset.tzinfo, WARSAW)

    def test_unknown_tz_falls_back_to_the_default(self):
        _scope, reset = quota.parse(
            "You've hit your session limit · resets 4am (Mars/Olympus)",
            NOW, "Europe/Warsaw")
        self.assertEqual(reset.tzinfo, WARSAW)

    def test_an_ordinary_failure_is_not_a_limit(self):
        self.assertIsNone(quota.parse("Error: connection reset by peer", NOW))
        self.assertIsNone(quota.parse("", NOW))
        self.assertIsNone(quota.parse(None, NOW))


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "personal"

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_then_blocked(self):
        entry = quota.record(self.state, "fable", SESSION_MSG, NOW,
                             "Europe/Warsaw")
        self.assertEqual(entry["scope"], "session")
        blocked = quota.blocked(self.state, NOW)
        self.assertEqual(list(blocked), ["fable"])
        self.assertEqual(blocked["fable"],
                         datetime(2026, 9, 9, 4, 20, tzinfo=WARSAW))

    def test_only_the_recorded_family_is_blocked(self):
        # The 2026-09-09 night: Fable dead, Sonnet running fine in the same
        # window. A shared block would have wasted the rest of the night.
        quota.record(self.state, "fable", SESSION_MSG, NOW, "Europe/Warsaw")
        self.assertNotIn("sonnet", quota.blocked(self.state, NOW))

    def test_an_ordinary_failure_costs_no_eligibility(self):
        self.assertIsNone(quota.record(self.state, "fable", "boom", NOW))
        self.assertEqual(quota.blocked(self.state, NOW), {})

    def test_expired_record_no_longer_blocks_but_is_kept(self):
        quota.record(self.state, "fable", SESSION_MSG, NOW, "Europe/Warsaw")
        later = datetime(2026, 9, 9, 4, 25, tzinfo=WARSAW)
        self.assertEqual(quota.blocked(self.state, later), {})
        self.assertIn("fable", quota.history(self.state))

    def test_record_fallback_blocks_for_one_window(self):
        entry = quota.record_fallback(self.state, "opus", NOW)
        self.assertEqual(entry["scope"], "unknown")
        self.assertEqual(quota.blocked(self.state, NOW)["opus"],
                         NOW + timedelta(hours=5))

    def test_a_later_record_replaces_the_earlier_one(self):
        quota.record(self.state, "fable", SESSION_MSG, NOW, "Europe/Warsaw")
        quota.record(self.state, "fable", WEEKLY_MSG, NOW, "Europe/Warsaw")
        self.assertEqual(quota.history(self.state)["fable"]["scope"], "weekly")

    def test_missing_and_corrupt_state_are_empty(self):
        self.assertEqual(quota.blocked(self.state, NOW), {})
        self.state.mkdir(parents=True)
        (self.state / quota.STATE_FILE).write_text("{not json")
        self.assertEqual(quota.blocked(self.state, NOW), {})

    def test_malformed_entry_is_ignored(self):
        self.state.mkdir(parents=True)
        (self.state / quota.STATE_FILE).write_text(
            json.dumps({"fable": {"until": "not-a-date"}, "opus": {}}))
        self.assertEqual(quota.blocked(self.state, NOW), {})


class TestCli(unittest.TestCase):
    """run.sh calls this after a non-zero session exit, with that slot's stderr."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state = self.dir / "personal"
        self.err = self.dir / "err.1.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ORCH / "lib" / "quota.py"), *args],
            capture_output=True, text=True, cwd=ORCH)

    def test_records_and_reports_a_limit(self):
        self.err.write_text(SESSION_MSG)
        r = self.run_cli("record", str(self.state), "fable", str(self.err))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("quota exhausted model=fable scope=session", r.stdout)
        self.assertIn("fable", quota.history(self.state))

    def test_stays_quiet_on_an_ordinary_failure(self):
        self.err.write_text("Traceback: something else")
        r = self.run_cli("record", str(self.state), "fable", str(self.err))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertEqual(quota.history(self.state), {})

    def test_missing_stderr_file_is_not_an_error(self):
        r = self.run_cli("record", str(self.state), "fable",
                         str(self.dir / "gone.txt"))
        self.assertEqual(r.returncode, 0)

    def test_bad_usage_reports_it(self):
        r = self.run_cli("nonsense")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stdout)


if __name__ == "__main__":
    unittest.main()
