import fcntl
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ORCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH))
from lib import ratelimits  # noqa: E402

TZ = "Europe/Warsaw"
UTC = timezone.utc
# A Thursday midday; the week resets a few days later.
T0 = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
WEEK_RESET = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
SEED = {"ts": "2026-09-24T21:10:00+00:00", "seed": True, "weekly_cap_usd": 850.0,
        "window_cap_usd": 58.0, "p90_daily_usd": 114.0}
EARLY_SEED = dict(SEED, ts="2026-09-01T00:00:00+00:00")


def win(pct, resets, usd):
    return {"used_percentage": pct, "resets_at": resets.isoformat(), "engine_usd": usd}


def weekly(ts, pct, usd, resets=WEEK_RESET):
    return {"ts": ts.isoformat(), "seven_day": win(pct, resets, usd)}


def payload(week_pct=None, week_resets=None, win_pct=None, win_resets=None):
    limits = {}
    if week_pct is not None:
        limits["seven_day"] = {"used_percentage": week_pct,
                               "resets_at": int(week_resets.timestamp())}
    if win_pct is not None:
        limits["five_hour"] = {"used_percentage": win_pct,
                               "resets_at": int(win_resets.timestamp())}
    return {"rate_limits": limits} if limits else {}


class TestCaps(unittest.TestCase):
    def weekly_detail(self, rows, now=T0 + timedelta(days=1)):
        return ratelimits.caps([EARLY_SEED] + rows, now, TZ)

    def test_cap_derivation_weekly_and_window_independently(self):
        row = weekly(T0, 36, 300.0)
        row["five_hour"] = win(50, T0 + timedelta(hours=2), 30.0)
        c = self.weekly_detail([row], now=T0 + timedelta(minutes=5))
        self.assertAlmostEqual(c["weekly_cap_usd"], 833.3333, places=3)
        self.assertAlmostEqual(c["window_cap_usd"], 60.0)

    def test_weekly_under_10_percent_is_ignored(self):
        c = self.weekly_detail([weekly(T0, 9.9, 99.0)])
        self.assertEqual(c["weekly_cap_usd"], 850.0)
        self.assertEqual(c["detail"]["weekly_cap_usd"]["source"], "seed")

    def test_weekly_at_10_percent_counts(self):
        c = self.weekly_detail([weekly(T0, 10, 100.0)])
        self.assertAlmostEqual(c["weekly_cap_usd"], 1000.0)

    def test_window_under_20_percent_is_ignored(self):
        row = {"ts": T0.isoformat(), "five_hour": win(19.9, T0 + timedelta(hours=2), 10.0)}
        c = self.weekly_detail([row], now=T0 + timedelta(minutes=5))
        self.assertEqual(c["window_cap_usd"], 58.0)

    def test_window_at_20_percent_counts(self):
        row = {"ts": T0.isoformat(), "five_hour": win(20, T0 + timedelta(hours=2), 10.0)}
        c = self.weekly_detail([row], now=T0 + timedelta(minutes=5))
        self.assertAlmostEqual(c["window_cap_usd"], 50.0)

    def test_reading_with_zero_engine_usd_is_ignored(self):
        c = self.weekly_detail([weekly(T0, 40, 0.0)])
        self.assertEqual(c["weekly_cap_usd"], 850.0)

    def test_median_of_the_day_not_mean_or_last(self):
        # median 830, mean 876.67, last 800.
        rows = [weekly(T0, 10, 83.0), weekly(T0 + timedelta(hours=1), 10, 100.0),
                weekly(T0 + timedelta(hours=2), 10, 80.0)]
        c = self.weekly_detail(rows)
        self.assertAlmostEqual(c["weekly_cap_usd"], 830.0)

    def test_source_is_reading_while_period_open_then_history(self):
        rows = [weekly(T0, 40, 400.0)]
        open_ = self.weekly_detail(rows, now=WEEK_RESET - timedelta(minutes=1))
        self.assertEqual(open_["detail"]["weekly_cap_usd"]["source"], "reading")
        closed = self.weekly_detail(rows, now=WEEK_RESET + timedelta(minutes=1))
        self.assertEqual(closed["detail"]["weekly_cap_usd"]["source"], "history")
        self.assertAlmostEqual(closed["weekly_cap_usd"], 1000.0)

    def test_row_at_or_after_its_own_resets_at_is_ignored(self):
        c = self.weekly_detail([weekly(WEEK_RESET, 40, 400.0)],
                               now=WEEK_RESET + timedelta(hours=1))
        self.assertEqual(c["weekly_cap_usd"], 850.0)

    def test_day_within_15_percent_is_median_of_days_no_change(self):
        d1 = weekly(T0, 10, 100.0)                           # 1000
        d2 = weekly(T0 + timedelta(days=1), 10, 90.0)        # 900
        now = T0 + timedelta(days=2)
        c = self.weekly_detail([d1, d2], now=now)
        self.assertAlmostEqual(c["weekly_cap_usd"], 950.0)
        whys = [x["why"] for x in ratelimits.recent_changes(c, now)
                if x["cap"] == "weekly_cap_usd"]
        self.assertEqual(whys, ["seed superseded"])

    def test_day_beyond_15_percent_replaces_cap_and_is_reported(self):
        d1 = weekly(T0, 10, 100.0)                           # 1000
        d2 = weekly(T0 + timedelta(days=1), 10, 110.0)       # 1100, within
        d3 = weekly(T0 + timedelta(days=2), 10, 60.0)        # 600, change
        d4 = weekly(T0 + timedelta(days=3), 10, 62.0)        # 620, within 600
        now = T0 + timedelta(days=3, hours=1)
        c = self.weekly_detail([d1, d2, d3], now=now)
        self.assertEqual(c["weekly_cap_usd"], 600.0)
        changes = [x for x in ratelimits.recent_changes(c, now)
                   if x["why"] == "limit change"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["cap"], "weekly_cap_usd")
        self.assertEqual(changes[0]["to"], 600.0)
        self.assertAlmostEqual(changes[0]["from"], 1050.0)
        # Older days no longer count: median of 600 and 620 only.
        c = self.weekly_detail([d1, d2, d3, d4], now=now)
        self.assertAlmostEqual(c["weekly_cap_usd"], 610.0)

    def test_fallback_to_history_when_readings_stop(self):
        last = T0 + timedelta(hours=3)
        rows = [weekly(T0, 20, 200.0), weekly(last, 30, 300.0)]
        now = WEEK_RESET + timedelta(days=3)
        d = self.weekly_detail(rows, now=now)["detail"]["weekly_cap_usd"]
        self.assertAlmostEqual(d["cap"], 1000.0)
        self.assertEqual(d["source"], "history")
        self.assertEqual(d["as_of"], last)


class TestSeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "acct"

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_writes_only_into_an_empty_history(self):
        self.assertTrue(ratelimits.seed(self.state, 850, 58, 114, SEED["ts"]))
        self.assertFalse(ratelimits.seed(self.state, 1, 2, 3, SEED["ts"]))
        rows = ratelimits.load(self.state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["weekly_cap_usd"], 850.0)

    def test_seed_still_writes_when_only_real_readings_exist(self):
        ratelimits._append(self.state, weekly(T0, 40, 400.0))
        self.assertTrue(ratelimits.seed(self.state, 850, 58, 114, SEED["ts"]))
        self.assertEqual(len(ratelimits.load(self.state)), 2)

    def test_seed_only_gives_seed_values(self):
        c = ratelimits.caps([SEED], T0, TZ)
        self.assertEqual((c["weekly_cap_usd"], c["window_cap_usd"], c["p90_daily_usd"]),
                         (850.0, 58.0, 114.0))
        for d in c["detail"].values():
            self.assertEqual(d["source"], "seed")

    def test_first_reading_supersedes_seed_even_within_15_percent(self):
        now = T0 + timedelta(hours=1)
        c = ratelimits.caps([EARLY_SEED, weekly(T0, 10, 80.0)], now, TZ)   # 800
        self.assertAlmostEqual(c["weekly_cap_usd"], 800.0)
        ch = ratelimits.recent_changes(c, now)
        self.assertEqual([x["why"] for x in ch], ["seed superseded"])

    def test_no_seed_row_gives_none(self):
        self.assertIsNone(ratelimits.caps([weekly(T0, 40, 400.0)], T0, TZ))

    def test_p90_scales_with_the_weekly_cap(self):
        c = ratelimits.caps([EARLY_SEED, weekly(T0, 40, 170.0)], T0 + timedelta(hours=1), TZ)
        self.assertAlmostEqual(c["weekly_cap_usd"], 425.0)
        self.assertAlmostEqual(c["p90_daily_usd"], 57.0)


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "acct"
        self.acct = {"name": "acct", "claude_config_dir": "/nonexistent"}

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, p, now, entries=()):
        return ratelimits.record(self.acct, self.state, p, now, entries=list(entries))

    def test_no_rate_limits_writes_nothing(self):
        self.assertIsNone(self.rec({"model": {}}, T0))
        self.assertEqual(ratelimits.load(self.state), [])

    def test_engine_usd_summed_over_each_period_only(self):
        win_reset = T0 + timedelta(hours=2)
        entries = [
            (WEEK_RESET - timedelta(days=7, minutes=1), "m", 1000.0, 0),  # before week
            (WEEK_RESET - timedelta(days=6), "m", 10.0, 0),               # week only
            (win_reset - timedelta(hours=5, minutes=1), "m", 100.0, 0),   # week only
            (win_reset - timedelta(hours=4), "m", 3.0, 0),                # both
            (T0 + timedelta(minutes=1), "m", 5000.0, 0),                  # after now
        ]
        row = self.rec(payload(40, WEEK_RESET, 50, win_reset), T0, entries)
        self.assertEqual(row["seven_day"]["engine_usd"], 113.0)
        self.assertEqual(row["five_hour"]["engine_usd"], 3.0)
        self.assertEqual(row["seven_day"]["resets_at"], WEEK_RESET.isoformat())
        self.assertTrue(row["seven_day"]["resets_at"].endswith("+00:00"))

    def test_window_absent_from_payload_is_absent_from_row(self):
        row = self.rec(payload(40, WEEK_RESET), T0)
        self.assertNotIn("five_hour", row)
        self.assertEqual(ratelimits.load(self.state), [row])

    def test_throttle_under_one_minute(self):
        self.rec(payload(40, WEEK_RESET), T0)
        self.assertIsNone(self.rec(payload(41, WEEK_RESET), T0 + timedelta(seconds=59)))
        self.assertEqual(len(ratelimits.load(self.state)), 1)

    def test_unchanged_values_write_nothing_even_after_a_minute(self):
        self.rec(payload(40, WEEK_RESET, 50, T0 + timedelta(hours=2)), T0)
        self.assertIsNone(self.rec(payload(40, WEEK_RESET, 50, T0 + timedelta(hours=2)),
                                   T0 + timedelta(minutes=5)))
        self.assertEqual(len(ratelimits.load(self.state)), 1)

    def test_changed_value_after_a_minute_writes(self):
        self.rec(payload(40, WEEK_RESET), T0)
        self.assertIsNotNone(self.rec(payload(41, WEEK_RESET), T0 + timedelta(minutes=1)))
        self.assertEqual(len(ratelimits.load(self.state)), 2)

    def test_concurrent_record_while_locked_returns_none(self):
        self.state.mkdir(parents=True)
        with open(self.state / ratelimits.LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIsNone(self.rec(payload(40, WEEK_RESET), T0))
        self.assertEqual(ratelimits.load(self.state), [])


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"ORCH_ROOT": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, account):
        return Path(self.tmp.name) / "orchestrator" / "state" / account

    def run_record(self, account, stdin):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", out):
            rc = ratelimits._main(["x", "record", account])
        return rc, out.getvalue()

    def test_record_bad_json_writes_err_file_silently(self):
        # The CLI reads the config of the ORCH_ROOT backlog, like gate.py.
        (Path(self.tmp.name) / "config.yaml").write_text(
            "accounts:\n  - name: acct\n    claude_config_dir: /nonexistent\n")
        name = "acct"
        rc, out = self.run_record(name, "not json")
        self.assertEqual((rc, out), (0, ""))
        err = (self.state(name) / ratelimits.ERROR).read_text()
        self.assertIn("JSONDecodeError", err)

    def test_record_unknown_account_writes_err_file_silently(self):
        # A status line must survive any failure, including a bad account name.
        rc, out = self.run_record("no-such-account", "{}")
        self.assertEqual((rc, out), (0, ""))
        err = (self.state("no-such-account") / ratelimits.ERROR).read_text()
        self.assertIn("unknown account", err)

    def test_seed_cli_seeds_once(self):
        argv = ["x", "seed", "acct", "850", "58", "114", "2026-09-24T23:10:00+02:00"]
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(ratelimits._main(argv), 0)
            self.assertEqual(ratelimits._main(argv), 0)
        rows = ratelimits.load(self.state("acct"))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["weekly_cap_usd"], rows[0]["window_cap_usd"],
                          rows[0]["p90_daily_usd"]), (850.0, 58.0, 114.0))


if __name__ == "__main__":
    unittest.main()
