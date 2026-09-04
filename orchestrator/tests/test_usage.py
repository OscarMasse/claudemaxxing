import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
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


if __name__ == "__main__":
    unittest.main()
