# 0013. Pace the week with a decaying reserve of one heavy (P90) day per remaining day

- Status: accepted (retroactive)
- Date: 2026-09-04

## Context

The engine runs background sessions on a subscription whose quota resets weekly, on the same account the owner uses interactively.
Interactive work has absolute priority: background work may only eat what the owner will not need before the reset (maintainer's notes; `docs/design.md`, "Quota-aware scheduling").
Quota left unspent at the reset is lost, so reserving too much wastes it just as surely as spending too much blocks the owner (`docs/design.md`, "Scheduling regimes", pre-reset burn-down paragraph).
The controller predates the public release: it is already in the initial commit `568f33a`, with the reserve then expressed in tokens (`p90_daily_tokens`).
The maintainer's notes record it as part of the original orchestrator design, before any commit; no exact decision day is in the repository, so this ADR takes the release date.

## Decision

On every tick, the background system may spend only `available = weekly_cap - consumed - p90_daily * days_remaining`, where `p90_daily` is a heavy (90th percentile) day of the owner's own usage and `days_remaining` is the fractional number of days to the next reset (`orchestrator/lib/controller.py:50-57`, `:226-227`).
If `available <= 0` the night tick skips with the reserve in its log line (`orchestrator/lib/controller.py:229-232`).
The reserve decays linearly to zero at the reset, so the week starts protective and ends fully released (`docs/design.md`, "Budget controller").
The controller keeps no memory: `available` is recomputed from measured consumption on every tick, so a heavy interactive day shrinks every later night and a quiet one grows it (`docs/design.md`, "Per-night allocation").
How that surplus is split between nights is a separate decision (inventory row 0014), and the pre-reset burn-down ignores the reserve entirely (inventory row 0026).
The P90 value was first hand-configured (`p90_daily_tokens`, then `p90_daily_usd`); it is now seeded once and scaled with the derived weekly cap (`orchestrator/lib/ratelimits.py:30`, `:233`).

## Alternatives considered

- **A fixed daily background allowance (weekly surplus divided by seven).** It ignores what the owner actually consumed, so a heavy interactive week could still be starved; reason for rejection not recorded beyond the priority rule above.
- **A reserve equal to the mean day, or a flat safety fraction of the cap.** Not recorded as considered; why the 90th percentile rather than another percentile is not recorded.
- **Predict the owner's remaining usage from a model of their history.** Not recorded as considered.
- **Strongest argument against.** (Own analysis.) Reserving a P90 day for every remaining day assumes every remaining day is heavy, which by construction is true of only one day in ten, so early in the week the reserve is roughly the sum of the ten worst days' worth and holds back quota the owner will almost never use; the engine then depends on the pre-reset burn-down to recover it, and anything the last night cannot absorb in its few windows is lost. A percentile of history is also a lagging predictor: when the owner's usage shifts (a crunch week, a new heavier model), the seeded P90 is wrong in whichever direction hurts, and only the per-tick re-measurement of `consumed` limits the damage.

**Would we decide the same today?** Yes - the rule is one line, needs no forecast, and fails safe for the owner, who has absolute priority; its over-reservation early in the week is recovered by back-loading and the burn-down (inventory rows 0014, 0026), and its input now tracks the derived weekly cap instead of a stale hand value.

## Consequences

- Good: the owner's worst plausible remaining week is protected at every point of the week, without forecasting.
- Good: stateless and deterministic in its inputs, so it is testable and every skip explains itself (`orchestrator/lib/controller.py:1-13`).
- Bad: early-week nights are small by design, and the week's useful background work concentrates late, which makes the last nights and the burn-down load-bearing.
- Bad: the protection is only as good as the P90 figure; usage from other devices is invisible to the measurement (`docs/design.md`, "Caps from rate-limit readings").

## Open questions

- Why the 90th percentile, and over what history window it was first computed, is not recorded.
- Which alternative pacing rules, if any, were weighed is not recorded.

## Sources

- `orchestrator/lib/controller.py:1-13`, `:50-57`, `:226-232`.
- `orchestrator/lib/ratelimits.py:30`, `:233`.
- `docs/design.md`, "Quota-aware scheduling" (line 47), "Budget controller" (line 160), "Per-night allocation" (line 177).
- `README.md:30`.
- Commit `568f33a` (initial public release, controller already present).
- Maintainer's notes (quota strategy).
