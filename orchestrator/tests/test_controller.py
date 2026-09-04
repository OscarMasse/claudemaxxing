import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from lib import controller

TZ = ZoneInfo("Europe/Warsaw")

# Test config uses reset_weekday 3 (Thursday) with 2026-08 dates where
# Aug 10 = Monday, Aug 13 = Thursday. The real config uses weekday 4; the
# controller only sees the config value, so the math under test is identical.
CFG = {
    "weekly_cap_tokens": 100, "promo_multiplier": 1.0, "promo_until": "2000-01-01",
    "p90_daily_tokens": 10, "window_cap_tokens": 15,
    "night_start": "02:00", "night_end": "06:00", "morning_guard": "08:30",
    "prereset_burn_hours": 8,
    "activity_idle_day_min": 60, "activity_idle_night_min": 40,
    "day_slice_min": 15, "night_slice_min": 50, "day_window_max_frac": 0.4,
    "reset_weekday": 3, "reset_time": "05:59", "reset_tz": "Europe/Warsaw",
    "fable_min_surplus_tokens": 50, "opus_min_surplus_tokens": 20,
}


def usage(week=0, block=None):
    return {"week_tokens": week, "block": block}


class TestReset(unittest.TestCase):
    def test_next_reset_from_monday(self):
        now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)  # Monday
        self.assertEqual(controller.next_reset(CFG, now),
                         datetime(2026, 8, 13, 5, 59, tzinfo=TZ))  # Thursday

    def test_next_reset_thursday_after_reset_time(self):
        now = datetime(2026, 8, 13, 6, 30, tzinfo=TZ)  # Thursday post-reset
        self.assertEqual(controller.next_reset(CFG, now),
                         datetime(2026, 8, 20, 5, 59, tzinfo=TZ))

    def test_prev_reset(self):
        now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        self.assertEqual(controller.prev_reset(CFG, now),
                         datetime(2026, 8, 6, 5, 59, tzinfo=TZ))


class TestDecide(unittest.TestCase):
    def test_full_week_never_runs(self):
        # Monday 14:00, week nearly consumed -> available <= 0 -> skip, any regime.
        now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=95), idle_min=999)
        self.assertEqual(d.action, "skip")
        self.assertIn("available", d.reason)

    def test_day_surplus_not_armed_early_week(self):
        # Monday 14:00, week=50: available = 100-50-26.6 = 23.4 > 0 but
        # 3 nights remain (Tue+Wed+Thu 02:00) x window_cap 15 = 45 > 23.4 -> skip.
        now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=50), idle_min=999)
        self.assertEqual(d.action, "skip")
        self.assertIn("nights", d.reason)

    def test_day_surplus_armed_empty_late_week(self):
        # Wednesday 15:00, week=0: reserve = 10*0.62 = 6.2, available = 93.8,
        # 1 night remains (Thu 02:00) x 15 = 15 < 93.8 -> surplus armed -> run.
        now = datetime(2026, 8, 12, 15, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=0), idle_min=999)
        self.assertEqual(d.action, "run")
        self.assertEqual(d.slice_min, 15)

    def test_day_surplus_blocked_by_activity(self):
        now = datetime(2026, 8, 12, 15, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=0), idle_min=30)
        self.assertEqual(d.action, "skip")
        self.assertIn("activity", d.reason)

    def test_day_surplus_blocked_by_window_usage(self):
        # Current window already 40%+ consumed -> leave it to the owner.
        now = datetime(2026, 8, 12, 15, 0, tzinfo=TZ)
        block = {"start": now - timedelta(hours=1), "end": now + timedelta(hours=4),
                 "tokens": 7, "active": True}  # 7 >= 0.4*15=6
        d = controller.decide(CFG, now, usage(week=0, block=block), idle_min=999)
        self.assertEqual(d.action, "skip")
        self.assertIn("window", d.reason)

    def test_night_runs_with_headroom(self):
        # Tuesday 02:30, empty week, no open window -> closes 07:30 < 08:30 guard.
        now = datetime(2026, 8, 11, 2, 30, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=0), idle_min=999)
        self.assertEqual(d.action, "run")
        self.assertEqual(d.slice_min, 50)

    def test_night_respects_morning_guard(self):
        # Tuesday 04:00: a fresh window would close at 09:00 > 08:30, but the open
        # block (23:30-04:30) leaves 30 min. Slice must shrink, not vanish.
        now = datetime(2026, 8, 11, 4, 0, tzinfo=TZ)
        block = {"start": now - timedelta(hours=4, minutes=30),
                 "end": now + timedelta(minutes=30), "tokens": 2, "active": True}
        d = controller.decide(CFG, now, usage(week=0, block=block), idle_min=999)
        self.assertEqual(d.action, "run")
        self.assertLessEqual(d.slice_min, 30)

    def test_night_skips_when_fresh_window_would_cross_guard(self):
        # Tuesday 04:00 with NO open window: fresh window 04:00-09:00 crosses 08:30.
        now = datetime(2026, 8, 11, 4, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=0), idle_min=999)
        self.assertEqual(d.action, "skip")
        self.assertIn("guard", d.reason)

    def test_night_activity_lock(self):
        now = datetime(2026, 8, 11, 2, 30, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=0), idle_min=20)  # < 40
        self.assertEqual(d.action, "skip")
        self.assertIn("activity", d.reason)

    def test_prereset_ignores_guard_and_reserve(self):
        # Wednesday 23:00, reset Thursday 05:59 (6h59m away < 8h) -> burn-down.
        # week=95: available with reserve would be <=0, but prereset drops the reserve.
        now = datetime(2026, 8, 12, 23, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=95), idle_min=999)
        self.assertEqual(d.action, "run")

    def test_prereset_still_respects_activity(self):
        now = datetime(2026, 8, 12, 23, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=0), idle_min=10)
        self.assertEqual(d.action, "skip")
        self.assertIn("activity", d.reason)

    def test_prereset_nothing_left(self):
        now = datetime(2026, 8, 12, 23, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=150), idle_min=999)
        self.assertEqual(d.action, "skip")

    def test_prereset_model_polarization(self):
        # Wednesday 23:00, reset in <8h. Model scales with the doomed surplus:
        # week=0 -> available 100 >= 50 -> fable; week=60 -> 40 -> opus;
        # week=90 -> 10 < 20 -> sonnet.
        now = datetime(2026, 8, 12, 23, 0, tzinfo=TZ)
        self.assertEqual(controller.decide(CFG, now, usage(week=0), idle_min=999).model, "fable")
        self.assertEqual(controller.decide(CFG, now, usage(week=60), idle_min=999).model, "opus")
        self.assertEqual(controller.decide(CFG, now, usage(week=90), idle_min=999).model, "sonnet")

    def test_night_and_day_never_upgrade(self):
        night = controller.decide(CFG, datetime(2026, 8, 11, 2, 30, tzinfo=TZ),
                                  usage(week=0), idle_min=999)
        self.assertEqual((night.action, night.model), ("run", "sonnet"))
        day = controller.decide(CFG, datetime(2026, 8, 12, 15, 0, tzinfo=TZ),
                                usage(week=0), idle_min=999)
        self.assertEqual((day.action, day.model), ("run", "sonnet"))

    def test_promo_multiplier_active(self):
        cfg = dict(CFG, promo_multiplier=1.5, promo_until="2026-08-19")
        # Wednesday 15:00, week=120: cap 150 with promo -> reserve 6.2 -> available
        # 23.8, 1 night x 15 < 23.8 -> run. Without promo it would skip (100-120<0).
        now = datetime(2026, 8, 12, 15, 0, tzinfo=TZ)
        d = controller.decide(cfg, now, usage(week=120), idle_min=999)
        self.assertEqual(d.action, "run")

    def test_promo_expired_falls_back(self):
        cfg = dict(CFG, promo_multiplier=1.5, promo_until="2026-08-11")
        now = datetime(2026, 8, 12, 15, 0, tzinfo=TZ)
        d = controller.decide(cfg, now, usage(week=120), idle_min=999)
        self.assertEqual(d.action, "skip")


if __name__ == "__main__":
    unittest.main()
