"""Pure decision math for the background orchestrator.

Everything here is deterministic in (cfg, now, usage, idle_min): no I/O, no clocks.
See the README sections "Scheduling regimes" and "Budget controller
(decaying reserve)".
"""
from collections import namedtuple
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

Decision = namedtuple("Decision", "action reason slice_min regime budget_tokens model",
                      defaults=("", 0, "sonnet"))

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


def _today_at(now, hhmm):
    t = _parse_hhmm(hhmm)
    return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def _prereset_model(cfg, available):
    """Polarized burn-down: the bigger the doomed surplus, the stronger the
    model. Fable/opus only ever run here (plus opus floors at night)."""
    if available >= float(cfg.get("fable_min_surplus_tokens", 100000000)):
        return "fable"
    if available >= float(cfg.get("opus_min_surplus_tokens", 30000000)):
        return "opus"
    return "sonnet"


def _window_headroom(cfg, block):
    """Tokens left in the current 5h window (full cap if no window is open)."""
    cap = float(cfg["window_cap_tokens"])
    if block and block.get("active"):
        return max(0.0, cap - float(block["tokens"]))
    return cap


def decide(cfg, now, usage, idle_min):
    reset = next_reset(cfg, now)
    cap = float(cfg["weekly_cap_tokens"]) * _promo(cfg, now)
    week = float(usage["week_tokens"])
    days_remaining = (reset - now).total_seconds() / 86400.0
    block = usage.get("block")
    idle = float("inf") if idle_min is None else idle_min

    prereset = (reset - now) <= timedelta(hours=float(cfg["prereset_burn_hours"]))
    night = (_today_at(now, cfg["night_start"]).timetz()
             <= now.timetz()
             < _today_at(now, cfg["night_end"]).timetz())

    if prereset:
        # Burn-down: no reserve, no morning guard; only the activity lock protects the owner.
        available = cap - week
        if available <= 0:
            return Decision("skip", f"prereset: nothing left (available={available:.0f})", 0, "prereset")
        if idle < float(cfg["activity_idle_night_min"]):
            return Decision("skip", f"prereset: activity {idle:.0f}min ago", 0, "prereset")
        minutes_to_reset = (reset - now).total_seconds() / 60
        budget = min(available, _window_headroom(cfg, block))
        return Decision("run", "prereset burn-down",
                        max(5, min(int(cfg["night_slice_min"]), int(minutes_to_reset))),
                        "prereset", budget, _prereset_model(cfg, available))

    reserve = float(cfg["p90_daily_tokens"]) * days_remaining
    available = cap - week - reserve

    if available <= 0:
        return Decision("skip", f"available={available:.0f} <= 0 (reserve={reserve:.0f})", 0,
                        "night" if night else "day")

    if night:
        if idle < float(cfg["activity_idle_night_min"]):
            return Decision("skip", f"night: activity {idle:.0f}min ago", 0, "night")
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
                            min(available, _window_headroom(cfg, block)))
        # Current window closes before the guard, but work crossing into a
        # follow-on window is only safe if that one also closes before the guard.
        slice_min = int(cfg["night_slice_min"])
        if window_end + WINDOW > guard:
            remainder_min = (window_end - now).total_seconds() / 60
            if remainder_min < 5:
                return Decision("skip", "night: window would cross morning guard", 0, "night")
            slice_min = min(slice_min, int(remainder_min))
        return Decision("run", "night regime", slice_min, "night",
                        min(available, _window_headroom(cfg, block)))

    # Daytime: surplus regime only.
    nights = _nights_remaining(cfg, now)
    night_capacity = nights * float(cfg["window_cap_tokens"])
    if available <= night_capacity:
        return Decision("skip",
                        f"day: {nights} nights ({night_capacity:.0f}) cover available ({available:.0f})", 0, "day")
    if idle < float(cfg["activity_idle_day_min"]):
        return Decision("skip", f"day: activity {idle:.0f}min ago", 0, "day")
    if block and block["active"] and block["tokens"] >= float(cfg["day_window_max_frac"]) * float(cfg["window_cap_tokens"]):
        return Decision("skip", "day: current window already serving the owner", 0, "day")
    return Decision("run", "day surplus regime", int(cfg["day_slice_min"]), "day",
                    min(available, _window_headroom(cfg, block)))
