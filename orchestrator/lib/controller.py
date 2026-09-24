"""Pure decision math for the background orchestrator.

Everything here is deterministic in (cfg, now, usage, idle_min): no I/O, no clocks.
See the README sections "Scheduling regimes" and "Budget controller
(decaying reserve)".

Two regimes only: the night window and the pre-reset burn-down. Daytime is
reserved for the owner, so the daytime tick always skips.

Every quantity here is USD at Anthropic list price (lib/transcripts.py has the
table and the 2026-09-12 measurement behind the unit). The math is the same as
when it ran on raw token counts; only the unit changed.
"""
import math

from collections import namedtuple
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# No `model` field: the model a session runs is the one its task declares
# (lib/tasks.py), never a property of the tick. The regime decides whether
# and how much to run, not what to run it on.
Decision = namedtuple("Decision", "action reason slice_min regime budget_usd",
                      defaults=("", 0))

WINDOW = timedelta(hours=5)  # a Max quota session window


def _parse_hhmm(s):
    h, m = str(s).split(":")
    return time(int(h), int(m))


def next_reset(cfg, now):
    tz = ZoneInfo(cfg["reset_tz"])
    local = now.astimezone(tz)
    t = _parse_hhmm(cfg["reset_time"])
    candidate = local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    days_ahead = (cfg["reset_weekday"] - local.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate


def prev_reset(cfg, now):
    return next_reset(cfg, now) - timedelta(days=7)


def _promo(cfg, now):
    tz = ZoneInfo(cfg["reset_tz"])
    until = date.fromisoformat(str(cfg["promo_until"]))
    if now.astimezone(tz).date() <= until:
        return float(cfg["promo_multiplier"])
    return 1.0


def surplus(cfg, now, week_usd):
    """USD the background system may spend this week: the weekly cap minus
    what is consumed minus the decaying daily reserve that protects the owner's
    own usage. Same quantity `decide()` calls `available`, exposed so the digest
    and the planner report exactly what the decision was made on."""
    cap = float(cfg["weekly_cap_usd"]) * _promo(cfg, now)
    days_remaining = (next_reset(cfg, now) - now).total_seconds() / 86400.0
    return cap - float(week_usd) - float(cfg["p90_daily_usd"]) * days_remaining


def _nights_remaining(cfg, now):
    """Occurrences of night_start strictly between now and the next reset."""
    t = _parse_hhmm(cfg["night_start"])
    reset = next_reset(cfg, now)
    n, probe = 0, now
    for _ in range(8):
        candidate = probe.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if candidate <= probe:
            candidate += timedelta(days=1)
        if candidate >= reset:
            break
        n += 1
        probe = candidate + timedelta(minutes=1)
    return n


def nights_remaining(cfg, now, in_night):
    """Night windows left before the reset, counting the one now in progress."""
    return _nights_remaining(cfg, now) + (1 if in_night else 0)


def night_capacity(cfg, last=False):
    """USD one night can physically absorb.

    A night (night_start..night_end) is shorter than a 5h quota window, so one
    window's cap is the binding ceiling: hoarding more than this for a later
    night strands it, because no night can spend it.

    The LAST night before the reset is not bounded that way. It falls inside
    the pre-reset burn-down, which runs for `prereset_burn_hours` and is
    bounded neither by night_end nor by the morning guard, so it spans
    `ceil(prereset_burn_hours / 5)` quota windows. Counting it as one window
    understates what the end of the week can burn and makes the plan spend
    earlier than it needs to - harmless for the total, but it hands cheap
    early nights quota that the valuable last one would have used better.
    """
    windows = 1
    if last:
        hours = float(cfg.get("prereset_burn_hours", 0))
        windows = max(1, math.ceil(hours / (WINDOW.total_seconds() / 3600.0)))
    return float(cfg["window_cap_usd"]) * windows


def night_budget(cfg, now, available, spent_tonight, in_night=True):
    """Tonight's USD allocation out of the weekly surplus.

    Deliberately non-linear and back-loaded: with `night_budget_ratio` r, night
    j of the n remaining gets a weight r^(j-1), so tonight takes the smallest
    slice ((r-1)/(r^n - 1) of the pool) and the last night before the reset
    takes the largest. Approaching the reset the surplus is worth less and less
    unspent, so the plan should spend it later rather than sooner.

    Two guards keep the escalation honest:

    - The last remaining night is open bar: the whole surplus, no share math
      (the `prereset` regime already does this within `prereset_burn_hours`;
      this makes the night regime agree with it a few hours earlier).
    - Quota that the remaining nights cannot physically absorb must burn
      tonight, or it is simply lost at the reset. Their capacity is one quota
      window each, except the last one, which spans the whole burn-down (see
      `night_capacity`).

    Adaptation is automatic and needs no memory: `available` is recomputed each
    tick from measured weekly usage, so a heavy interactive day shrinks every
    later night's allocation, and a quiet one grows it.

    `spent_tonight` is what this night has already burned. The share is taken
    on the pool as it stood at night start (`available + spent_tonight`), so
    the allocation does not shrink as the night progresses; only the remainder
    returned does.
    """
    n = nights_remaining(cfg, now, in_night)
    pool = available + spent_tonight
    if n <= 1:
        return max(0.0, pool - spent_tonight)  # last night: open bar
    r = float(cfg.get("night_budget_ratio", 2.0))
    if r <= 1.0:
        share = pool / n  # degenerate ratio: fall back to a flat split
    else:
        share = pool * (r - 1.0) / (r ** n - 1.0)
    # Never strand quota the later nights could not absorb anyway. Of the
    # n - 1 nights after tonight, exactly one is the last before the reset.
    later_capacity = ((n - 2) * night_capacity(cfg)
                      + night_capacity(cfg, last=True))
    unabsorbable = pool - later_capacity
    target = max(share, unabsorbable)
    target = min(target, night_capacity(cfg))
    return max(0.0, target - spent_tonight)


def _today_at(now, hhmm):
    t = _parse_hhmm(hhmm)
    return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def _window_headroom(cfg, block):
    """USD left in the current 5h window (full cap if no window is open)."""
    cap = float(cfg["window_cap_usd"])
    if block and block.get("active"):
        return max(0.0, cap - float(block["usd"]))
    return cap


def decide(cfg, now, usage, idle_min):
    reset = next_reset(cfg, now)
    cap = float(cfg["weekly_cap_usd"]) * _promo(cfg, now)
    week = float(usage["week_usd"])
    days_remaining = (reset - now).total_seconds() / 86400.0
    block = usage.get("block")
    idle = float("inf") if idle_min is None else idle_min

    prereset = (reset - now) <= timedelta(hours=float(cfg["prereset_burn_hours"]))
    night = (_today_at(now, cfg["night_start"]).timetz()
             <= now.timetz()
             < _today_at(now, cfg["night_end"]).timetz())

    if prereset:
        # Burn-down: no reserve, no budget, no model arbitration, no morning
        # guard, and no activity lock. The lock used to be the one guard kept
        # here; on 2026-09-24 it held the burn-down to one batch in an hour
        # with 69% of the week still unspent. With that much surplus left a
        # session clashing with the owner's own use is unlikely and cheap,
        # while every skipped tick is quota that expires at the reset.
        #
        # Measured weekly consumption is an estimate, and its error is only
        # tolerable because during the week it merely paces spending: an
        # overestimate costs a few sessions and the next tick re-measures.
        # On the last night that same error is the one thing standing between
        # a large unspent surplus and the work it could buy, and the surplus
        # is worth exactly zero once the reset passes. On 2026-09-10 the
        # estimate read 91% of the week consumed where `/usage` read about
        # 50%, so the tick capped itself at opus and skipped every
        # `model: fable` task for the fifth week running. At this hour there
        # is nothing left to protect, so nothing is predicted.
        #
        # The real limit is observed instead: a session that hits the
        # account's wall records it (lib/quota.py) and later ticks stop
        # launching that model. A task cut mid-slice is not a loss either -
        # it resumes at the start of the next week.
        minutes_to_reset = (reset - now).total_seconds() / 60
        return Decision("run", "prereset burn-down",
                        max(5, min(int(cfg["night_slice_min"]), int(minutes_to_reset))),
                        "prereset", float("inf"))

    if not night:
        # Outside the night window and outside the burn-down, the system does
        # not run at all. It used to have a "day surplus" regime, armed when
        # the remaining nights could not absorb the surplus; measured on the
        # live gatekeeper it armed 0 times in 75 day ticks, because the nights
        # plus the burn-down always cover the surplus first. Keeping a regime
        # that never fires only added config, code and a way to surprise the
        # owner mid-workday. Daytime runs are manual (`run.sh`) now.
        return Decision("skip", "day: daytime runs are manual only", 0, "day")

    reserve = float(cfg["p90_daily_usd"]) * days_remaining
    available = cap - week - reserve

    if available <= 0:
        return Decision("skip",
                        f"available={available:.2f} <= 0 (reserve={reserve:.2f})",
                        0, "night")

    if idle < float(cfg["activity_idle_night_min"]):
        return Decision("skip", f"night: activity {idle:.0f}min ago", 0, "night")
    # The night spends tonight's share of the surplus, not the whole weekly
    # surplus: without this the first night of the week drains everything
    # and the later (more valuable) nights find available <= 0.
    tonight = night_budget(cfg, now, available,
                           float(usage.get("spent_tonight") or 0.0), in_night=True)
    if tonight <= 0:
        return Decision("skip",
                        f"night: tonight's allocation spent "
                        f"(available={available:.2f})", 0, "night")
    guard = _today_at(now, cfg["morning_guard"])
    window_end = block["end"] if (block and block["active"]) else now + WINDOW
    if window_end > guard:
        if block and block["active"]:
            # An open late-evening window: use its remainder, never past the guard.
            remainder_min = (min(block["end"], guard) - now).total_seconds() / 60
        else:
            remainder_min = 0
        if remainder_min < 5:
            return Decision("skip", "night: window would cross morning guard", 0, "night")
        return Decision("run", "night: open-window remainder",
                        min(int(cfg["night_slice_min"]), int(remainder_min)), "night",
                        min(tonight, _window_headroom(cfg, block)))
    # Current window closes before the guard, but work crossing into a
    # follow-on window is only safe if that one also closes before the guard.
    slice_min = int(cfg["night_slice_min"])
    if window_end + WINDOW > guard:
        remainder_min = (window_end - now).total_seconds() / 60
        if remainder_min < 5:
            return Decision("skip", "night: window would cross morning guard", 0, "night")
        slice_min = min(slice_min, int(remainder_min))
    return Decision("run", "night regime", slice_min, "night",
                    min(tonight, _window_headroom(cfg, block)))
