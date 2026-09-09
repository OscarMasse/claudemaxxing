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

FIXTURE = {
    "blocks": [
        # Before the 2026-08-06 05:59 Warsaw reset (03:59 UTC): excluded from week.
        {"startTime": "2026-08-05T10:00:00.000Z", "endTime": "2026-08-05T15:00:00.000Z",
         "isActive": False, "isGap": False, "totalTokens": 111},
        # Gap entries must be ignored.
        {"startTime": "2026-08-07T00:00:00.000Z", "endTime": "2026-08-07T05:00:00.000Z",
         "isActive": False, "isGap": True, "totalTokens": 0},
        # In-week, closed.
        {"startTime": "2026-08-07T08:00:00.000Z", "endTime": "2026-08-07T13:00:00.000Z",
         "isActive": False, "isGap": False, "totalTokens": 40},
        # In-week, active.
        {"startTime": "2026-08-10T11:00:00.000Z", "endTime": "2026-08-10T16:00:00.000Z",
         "isActive": True, "isGap": False, "totalTokens": 7},
    ]
}

OTHER_FIXTURE = {
    "blocks": [
        {"startTime": "2026-08-07T08:00:00.000Z", "endTime": "2026-08-07T13:00:00.000Z",
         "isActive": False, "isGap": False, "totalTokens": 9},
    ]
}


def write_fixture(data):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


class TestUsage(unittest.TestCase):
    def setUp(self):
        os.environ["ORCH_CCUSAGE_JSON"] = write_fixture(FIXTURE)

    def tearDown(self):
        os.environ.pop("ORCH_CCUSAGE_JSON", None)
        os.environ.pop("ORCH_CCUSAGE_JSON_WORK", None)

    def test_snapshot(self):
        now = datetime(2026, 8, 10, 14, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        snap = usage.snapshot(CFG, now)
        self.assertEqual(snap["week_tokens"], 47)  # 40 + 7, pre-reset 111 excluded
        self.assertEqual(snap["account"], "personal")
        self.assertTrue(snap["block"]["active"])
        self.assertEqual(snap["block"]["tokens"], 7)
        self.assertEqual(snap["block"]["end"],
                         datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc))

    def test_per_account_fixture_override(self):
        # The account-specific env var beats the generic one, so two accounts
        # can be measured from two different fixtures in the same process.
        os.environ["ORCH_CCUSAGE_JSON_WORK"] = write_fixture(OTHER_FIXTURE)
        now = datetime(2026, 8, 10, 14, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        work = usage.snapshot(dict(CFG, name="work"), now)
        self.assertEqual(work["week_tokens"], 9)
        self.assertEqual(work["account"], "work")
        personal = usage.snapshot(CFG, now)
        self.assertEqual(personal["week_tokens"], 47)

    def test_env_name(self):
        self.assertEqual(usage.env_name("side-projects"), "SIDE_PROJECTS")
        self.assertEqual(usage.env_name("work"), "WORK")


SNAPSHOT_FIXTURE = {
    "week_tokens": 50,
    "week_by_family": {"sonnet": 30, "fable": 20},
    "block": {"start": "2026-08-10T11:20:00+00:00",
              "end": "2026-08-10T16:20:00+00:00",
              "tokens": 7,
              "by_family": {"fable": 7}},
    "unknown_models": ["weird-model"],
}


class TestSnapshotFixtureShape(unittest.TestCase):
    """The current fixture shape: a snapshot, as lib/transcripts.py returns it."""

    def setUp(self):
        os.environ["ORCH_USAGE_JSON"] = write_fixture(SNAPSHOT_FIXTURE)
        self.now = datetime(2026, 8, 10, 14, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    def tearDown(self):
        os.environ.pop("ORCH_USAGE_JSON", None)
        os.environ.pop("ORCH_USAGE_JSON_WORK", None)

    def test_snapshot(self):
        snap = usage.snapshot(CFG, self.now)
        self.assertEqual(snap["week_tokens"], 50)
        self.assertEqual(snap["week_by_family"], {"sonnet": 30, "fable": 20})
        self.assertEqual(snap["block"]["tokens"], 7)
        self.assertEqual(snap["block"]["by_family"], {"fable": 7})
        self.assertTrue(snap["block"]["active"])
        self.assertEqual(snap["block"]["end"],
                         datetime(2026, 8, 10, 16, 20, tzinfo=timezone.utc))
        self.assertEqual(snap["unknown_models"], ["weird-model"])

    def test_no_open_window(self):
        os.environ["ORCH_USAGE_JSON"] = write_fixture(
            {"week_tokens": 3, "block": None})
        snap = usage.snapshot(CFG, self.now)
        self.assertIsNone(snap["block"])
        self.assertEqual(snap["week_by_family"], {})

    def test_new_env_var_beats_the_legacy_one(self):
        os.environ["ORCH_CCUSAGE_JSON"] = write_fixture(FIXTURE)
        try:
            self.assertEqual(usage.snapshot(CFG, self.now)["week_tokens"], 50)
        finally:
            os.environ.pop("ORCH_CCUSAGE_JSON", None)

    def test_per_account_override(self):
        os.environ["ORCH_USAGE_JSON_WORK"] = write_fixture(
            {"week_tokens": 9, "block": None})
        self.assertEqual(
            usage.snapshot(dict(CFG, name="work"), self.now)["week_tokens"], 9)


class TestLiveTranscriptPath(unittest.TestCase):
    """No fixture: the snapshot is read from the account's own transcripts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for var in ("ORCH_USAGE_JSON", "ORCH_USAGE_JSON_PERSONAL",
                    "ORCH_CCUSAGE_JSON", "ORCH_CCUSAGE_JSON_PERSONAL"):
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
                        "usage": {"input_tokens": 12}},
        }) + "\n")
        snap = usage.snapshot(dict(CFG, claude_config_dir=str(cfg_dir)), now)
        self.assertEqual(snap["week_tokens"], 12)
        self.assertEqual(snap["week_by_family"], {"fable": 12})
        self.assertEqual(snap["block"]["by_family"], {"fable": 12})

    def test_an_account_that_has_never_run_measures_zero(self):
        now = datetime(2026, 8, 10, 14, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        snap = usage.snapshot(CFG, now)
        self.assertEqual(snap["week_tokens"], 0)
        self.assertIsNone(snap["block"])


if __name__ == "__main__":
    unittest.main()
