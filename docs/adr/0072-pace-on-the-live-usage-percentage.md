# 0072. Pace on the live `/usage` percentage; the derived cap is only a fallback

- Status: accepted
- Date: 2026-10-03

## Context

The engine records the account's live `used_percentage` for the week and the 5h window (status line and headless `rate_limit_event`, see 0017), but until now used it only to derive USD caps (`cap = engine_usd / pct`).
Every quota figure was then recomputed as `ledger USD / cap`.
On 2026-10-03 the digest said `usage_week_pct=12.3` while `/usage` said 9%.
The week had reset the day before, the noise rule dropped every reading under 10%, so the cap stayed on a 25h-old history value (993 USD, about 30% under the 1360 USD the live reading implied), and the night budget shrank accordingly.
The owner decided the same day: the direct measurement is the standard, the estimate is only a fallback.

## Decision

Pacing runs on the latest live percentage of each window when a reading of the still open period exists within a staleness bound (`lib/ratelimits.direct`), whatever its value: the under-10% / under-20% noise rules apply to cap derivation only.
Between readings the percentage is extrapolated with the engine's own spend since the reading, `pct = pct_reading + usd_since / cap`, so the cap estimate only covers the delta.
The controller keeps its USD arithmetic: the percentage is converted to `pct x cap` (`gate.snapshot`), so the reserve, `available`, the night budget and the window headroom all run on the measured share.
The 5h window reading also replaces the engine's block bounds with the account's own window (`resets_at`).
With no fresh reading, the ledger USD over the derived cap is used, as before.
`gate.py status` prints every percentage with `source=reading|extrapolated|estimate` and the reading age; the digest shows the direct figure first.

Staleness bounds (`STALE_AFTER`):

- 5h window: 5 hours, and the reading must belong to the open window.
- Week: 1 day. The extrapolation only adds what the engine itself spent; what it cannot see (the owner on another device, claude.ai) grows with the reading's age, and one day of it is what the p90 daily reserve already absorbs. Past a day the cap estimate is no worse.

## Alternatives considered

- **Lower the noise threshold so early-week readings derive a cap.** Rejected: a whole-percent reading at 2-9% carries a 5-25% error on the cap, and the estimate would still be the primary input.
- **Use the percentage only for display.** Rejected: the night budget is what the 30% error hurt.

## Consequences

- Good: pacing tracks `/usage` exactly at each reading, including usage the engine cannot see, and early-week readings are no longer discarded.
- Bad: the absolute USD figures (`week_usd`, `available`) still carry the derived cap's scale error; only the share is exact. A week without the owner's status line and without headless sessions falls back to the estimate after a day.

## Sources

- `orchestrator/lib/ratelimits.py` (`direct`, `STALE_AFTER`), `orchestrator/gate.py` (`snapshot`, `pace_label`), backlog task `claudemaxxing-quota-direct-reading-first`.
