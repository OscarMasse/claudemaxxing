# 0073. Weight the daily reserve by workday; off days keep a share

- Status: accepted
- Date: 2026-10-05

## Context

The reserve that protects the owner's own usage (0013) was `p90_daily_usd * days_remaining`, in calendar days.
The owner of the reference install does not work on Saturday and Sunday, and the weekly reset is Friday 05:59.
Right after the reset the engine therefore reserved about 7 heavy days while only about 5 of them were workdays.
Measured on 2026-10-04: 102 USD/day with a 762 USD cap, so about 714 USD reserved and about 48 USD left for 7 nights.
The weekend quota was not lost outright (the reserve decays, so later nights get it), but each night absorbs only about one 5h window, so the deferred quota piled onto the last nights, and what they could not absorb expired at the reset.
It also inverted the logic: weekend nights protect no workday the next morning, yet got the thinnest allocation.
The same formula was computed in three places (`surplus()`, `decide()`, the `gate.py status` print).

## Decision

One function owns the reserve, `controller.reserve(cfg, now) = p90_daily_usd * reserve_days(cfg, now)`.
`reserve_days` cuts the time to the next reset at local midnights in the reset timezone and sums each piece as a fraction of 24 hours, weighted 1.0 on a workday and `offday_reserve_ratio` on any other day, so the partial current day and the partial reset day are prorated.
Two config keys, top-level or per account: `workdays` (weekday names) and `offday_reserve_ratio` (in [0, 1], default 0.25).
An unknown weekday or an out-of-range ratio is refused by config validation.
Without `workdays`, every day weighs 1.0 and the reserve is exactly the old calendar-day value.
`gate.py status` prints `reserve_days`, `workday_equiv` and the workdays next to the reserve.

## Alternatives considered

- **Keep the flat calendar reserve.** Rejected: it starves the Friday and weekend nights every week for no protection.
- **Zero reserve on off days.** Rejected: the owner does use Claude on some weekends (this finding came from a Sunday session); a small share covers that.
- **Make workdays mon-fri the default.** Rejected: an open-source user with another schedule would silently lose protection; the default stays calendar days.
- **Measure `p90_daily_usd` from workday usage.** Out of scope here; the seed ratio stays.

## Consequences

- Good: after a Friday reset with mon-fri and 0.25 the reserve is about 5.5 heavy days instead of 7, freeing about 1.5 days of quota for the Friday and weekend nights; Monday to Thursday the reserve is unchanged.
- Bad: on a weekend the owner works hard, the reserve is thinner; the activity lock still blocks launches while they are active, and the ratio is tunable.

## Sources

- `orchestrator/lib/controller.py` (`reserve_days`, `reserve`), `orchestrator/lib/config.py` (`_check_reserve_key`), backlog task `claudemaxxing-reserve-workdays-only`.
