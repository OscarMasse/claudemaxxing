import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from lib import usage

CFG = {"name": "personal", "claude_config_dir": "/nonexistent",
       "reset_weekday": 3, "reset_time": "05:59", "reset_tz": "Europe/Warsaw"}

# The fixture shape: a snapshot, as lib/transcripts.summary() returns it,
# in USD at list price.
FIXTURE = {
    "week_usd": 50.0,
    "week_by_family": {"sonnet": 30.0, "fable": 20.0},
    "block": {"start": "2026-08-10T11:20:00+00:00",
              "end": "2026-08-10T16:20:00+00:00",
              "usd": 7.0,
              "by_family": {"fable": 7.0}},
    "unknown_models": ["weird-model"],
}


def write_fixture(data):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


class TestSnapshotFixture(unittest.TestCase):
    def setUp(self):
        os.environ["ORCH_USAGE_JSON"] = write_fixture(FIXTURE)
        self.now = datetime(2026, 8, 10, 14, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    def tearDown(self):
        os.environ.pop("ORCH_USAGE_JSON", None)
        os.environ.pop("ORCH_USAGE_JSON_WORK", None)

    def test_snapshot(self):
        snap = usage.snapshot(CFG, self.now)
        self.assertEqual(snap["account"], "personal")
        self.assertAlmostEqual(snap["week_usd"], 50.0)
        self.assertEqual(snap["week_by_family"], {"sonnet": 30.0, "fable": 20.0})
        self.assertAlmostEqual(snap["block"]["usd"], 7.0)
        self.assertEqual(snap["block"]["by_family"], {"fable": 7.0})
        self.assertTrue(snap["block"]["active"])
        self.assertEqual(snap["block"]["end"],
                         datetime(2026, 8, 10, 16, 20, tzinfo=timezone.utc))
        self.assertEqual(snap["unknown_models"], ["weird-model"])

    def test_no_open_window(self):
        os.environ["ORCH_USAGE_JSON"] = write_fixture(
            {"week_usd": 3, "block": None})
        snap = usage.snapshot(CFG, self.now)
        self.assertIsNone(snap["block"])
        self.assertEqual(snap["week_by_family"], {})
        self.assertAlmostEqual(snap["week_usd"], 3.0)

    def test_per_account_override(self):
        # The account-specific env var beats the generic one, so two accounts
        # can be measured from two different fixtures in the same process.
        os.environ["ORCH_USAGE_JSON_WORK"] = write_fixture(
            {"week_usd": 9, "block": None})
        self.assertAlmostEqual(
            usage.snapshot(dict(CFG, name="work"), self.now)["week_usd"], 9.0)
        self.assertAlmostEqual(usage.snapshot(CFG, self.now)["week_usd"], 50.0)

    def test_env_name(self):
        self.assertEqual(usage.env_name("side-projects"), "SIDE_PROJECTS")
        self.assertEqual(usage.env_name("work"), "WORK")


class TestLiveTranscriptPath(unittest.TestCase):
    """No fixture: the snapshot is read from the account's own transcripts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for var in ("ORCH_USAGE_JSON", "ORCH_USAGE_JSON_PERSONAL"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_the_accounts_own_config_dir(self):
        cfg_dir = Path(self.tmp.name) / "profile"
        proj = cfg_dir / "projects" / "slug"
        proj.mkdir(parents=True)
        now = datetime(2026, 8, 10, 14, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        ts = now - timedelta(hours=1)
        (proj / "a.jsonl").write_text(json.dumps({
            "timestamp": ts.astimezone(timezone.utc).isoformat(),
            "requestId": "r1",
            "message": {"id": "m1", "model": "claude-fable-5-1",
                        "usage": {"input_tokens": 1_000_000}},
        }) + "\n")
        snap = usage.snapshot(dict(CFG, claude_config_dir=str(cfg_dir)), now)
        self.assertAlmostEqual(snap["week_usd"], 10.0)  # 1M fable input tokens
        self.assertAlmostEqual(snap["week_by_family"]["fable"], 10.0)
        self.assertAlmostEqual(snap["block"]["by_family"]["fable"], 10.0)

    def test_an_account_that_has_never_run_measures_zero(self):
        now = datetime(2026, 8, 10, 14, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        snap = usage.snapshot(CFG, now)
        self.assertEqual(snap["week_usd"], 0)
        self.assertIsNone(snap["block"])


if __name__ == "__main__":
    unittest.main()
