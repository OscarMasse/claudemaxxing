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
    "activity_idle_night_min": 40, "night_slice_min": 50,
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
        # Tuesday 02:30, week nearly consumed -> available <= 0 -> skip.
        now = datetime(2026, 8, 11, 2, 30, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=95), idle_min=999)
        self.assertEqual(d.action, "skip")
        self.assertIn("available", d.reason)

    def test_daytime_never_runs_however_big_the_surplus(self):
        # Wednesday 15:00, week=0: the whole cap is available and only one
        # night is left to absorb it - the old day regime would have armed.
        now = datetime(2026, 8, 12, 15, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=0), idle_min=999)
        self.assertEqual((d.action, d.regime), ("skip", "day"))
        self.assertIn("manual", d.reason)

    def test_daytime_never_runs_even_with_the_owner_away(self):
        # An idle machine is not an invitation: the workday stays the owner's.
        now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=0), idle_min=100000)
        self.assertEqual((d.action, d.regime), ("skip", "day"))

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

    def test_night_never_upgrades_the_tick_model(self):
        # Only the burn-down polarizes the model; a plain night ticks at sonnet
        # and lets a task's `model:` floor raise its own session.
        night = controller.decide(CFG, datetime(2026, 8, 11, 2, 30, tzinfo=TZ),
                                  usage(week=0), idle_min=999)
        self.assertEqual((night.action, night.model), ("run", "sonnet"))

    def test_promo_multiplier_active(self):
        cfg = dict(CFG, promo_multiplier=1.5, promo_until="2026-08-19")
        # Wednesday 02:30, week=120: cap 150 with promo -> reserve 11.4 ->
        # available 18.6 -> run. Without promo it skips (100-120-11.4 < 0).
        now = datetime(2026, 8, 12, 2, 30, tzinfo=TZ)
        d = controller.decide(cfg, now, usage(week=120), idle_min=999)
        self.assertEqual(d.action, "run")

    def test_promo_expired_falls_back(self):
        cfg = dict(CFG, promo_multiplier=1.5, promo_until="2026-08-11")
        now = datetime(2026, 8, 12, 2, 30, tzinfo=TZ)
        d = controller.decide(cfg, now, usage(week=120), idle_min=999)
        self.assertEqual(d.action, "skip")


class TestNightBudget(unittest.TestCase):
    """Per-night allocation of the weekly surplus (non-linear, back-loaded)."""

    def nights(self, n):
        """A `now` inside the night with exactly `n` nights left (this one
        included). Reset is Thursday 05:59 and night_start 02:00, so the last
        night is Thursday's 02:00-05:59 sliver."""
        return datetime(2026, 8, 14 - n, 3, 0, tzinfo=TZ)

    def test_back_loaded_share_grows_toward_the_reset(self):
        pool = 100
        shares = [controller.night_budget(CFG, self.nights(n), pool, 0)
                  for n in (5, 4, 3, 2)]
        self.assertEqual(shares, sorted(shares),
                         f"allocation must grow as the reset nears: {shares}")

    def test_last_night_is_open_bar(self):
        # One night left: the whole surplus, no share math and no night cap.
        cfg = dict(CFG, window_cap_tokens=15)
        self.assertEqual(controller.night_budget(cfg, self.nights(1), 100, 0), 100)

    def test_share_capped_by_what_one_night_can_absorb(self):
        # A huge surplus early in the week: tonight cannot exceed one window.
        got = controller.night_budget(CFG, self.nights(5), 10000, 0)
        self.assertEqual(got, float(CFG["window_cap_tokens"]))

    def test_unabsorbable_quota_burns_tonight(self):
        # 3 nights left: the middle one absorbs 15, the last one 30 (two
        # windows of burn-down), so 45 of the 55 pool has a later home and the
        # remaining 10 is doomed unless it burns tonight.
        cfg = dict(CFG, night_budget_ratio=2.0)
        self.assertEqual(controller.night_budget(cfg, self.nights(3), 55, 0), 10)
        # Geometric share alone would have been far smaller than that floor.
        self.assertLess(55 * (2 - 1) / (2 ** 3 - 1), 10)

    def test_last_night_absorbs_the_whole_burn_down_not_one_window(self):
        # The last night is the pre-reset burn-down: 8h spans two 5h quota
        # windows, and it is bounded by neither night_end nor the morning
        # guard. Counting it as one window would make the plan hoard less for
        # it than it can actually spend.
        self.assertEqual(controller.night_capacity(CFG), 15)
        self.assertEqual(controller.night_capacity(CFG, last=True), 30)
        # A burn-down shorter than one window is still one window.
        self.assertEqual(
            controller.night_capacity(dict(CFG, prereset_burn_hours=3), last=True), 15)

    def test_spent_tonight_is_a_remainder_not_a_new_grant(self):
        # The share is taken on the pool as it stood at night start, so two
        # ticks of the same night cannot each grant a full share.
        first = controller.night_budget(CFG, self.nights(4), 40, 0)
        second = controller.night_budget(CFG, self.nights(4), 40 - first, first)
        self.assertAlmostEqual(second, 0.0)

    def test_flat_split_when_ratio_is_degenerate(self):
        cfg = dict(CFG, night_budget_ratio=1.0)
        self.assertAlmostEqual(controller.night_budget(cfg, self.nights(4), 40, 0),
                               10.0)

    def test_night_decision_uses_tonight_not_the_weekly_surplus(self):
        # Monday 03:00, week=0: available is 100 - reserve, but the night may
        # only spend its own share, which one window also caps.
        now = datetime(2026, 8, 10, 3, 0, tzinfo=TZ)
        d = controller.decide(CFG, now, usage(week=0), idle_min=999)
        self.assertEqual(d.action, "run")
        self.assertLessEqual(d.budget_tokens, float(CFG["window_cap_tokens"]))

    def test_night_skips_once_tonight_is_spent(self):
        now = datetime(2026, 8, 10, 3, 0, tzinfo=TZ)
        u = usage(week=30)
        u["spent_tonight"] = 30
        d = controller.decide(CFG, now, u, idle_min=999)
        self.assertEqual(d.action, "skip")
        self.assertIn("tonight's allocation", d.reason)

    def test_nights_remaining_counts_the_one_in_progress(self):
        # Wednesday 03:00: Thursday's pre-reset sliver is still to come.
        now = datetime(2026, 8, 12, 3, 0, tzinfo=TZ)
        self.assertEqual(controller.nights_remaining(CFG, now, True), 2)
        self.assertEqual(controller.nights_remaining(CFG, now, False), 1)


class TestSurplus(unittest.TestCase):
    def test_surplus_is_cap_minus_usage_minus_decaying_reserve(self):
        now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)  # Monday, ~2.66d to reset
        days = (controller.next_reset(CFG, now) - now).total_seconds() / 86400
        self.assertAlmostEqual(controller.surplus(CFG, now, 40),
                               100 - 40 - 10 * days)

    def test_surplus_matches_what_decide_acts_on(self):
        # The reserve alone must sink it: same number, one source of truth.
        now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        self.assertLess(controller.surplus(CFG, now, 95), 0)
        self.assertEqual(controller.decide(CFG, now, usage(week=95),
                                           idle_min=999).action, "skip")

    def test_surplus_honors_the_promo_multiplier(self):
        cfg = dict(CFG, promo_multiplier=1.5, promo_until="2026-08-19")
        now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        self.assertAlmostEqual(controller.surplus(cfg, now, 0) -
                               controller.surplus(CFG, now, 0), 50)


if __name__ == "__main__":
    unittest.main()
