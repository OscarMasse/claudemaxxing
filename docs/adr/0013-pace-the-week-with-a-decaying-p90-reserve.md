# 0013. Pace the week with a decaying reserve of one heavy (P90) day per remaining day

- Status: accepted (retroactive)
- Date: 2026-08-12

## Context

The engine runs background sessions on a subscription whose quota resets weekly, on the same account the owner uses interactively.
Interactive work has absolute priority: background work may only eat what the owner will not need before the reset (maintainer's notes; `docs/design.md`, "Quota-aware scheduling").
Quota left unspent at the reset is lost, so reserving too much wastes it just as surely as spending too much blocks the owner (`docs/design.md`, "Scheduling regimes", pre-reset burn-down paragraph).
The controller predates the public release: it is already in the initial commit `568f33a`, with the reserve then expressed in tokens (`p90_daily_tokens`).
The maintainer's notes record the decaying-reserve controller in the orchestrator design of 2026-08-12, before any commit.

## Decision

On every tick, the background system may spend only `available = weekly_cap - consumed - p90_daily * days_remaining`, where `p90_daily` is a heavy (90th percentile) day of the owner's own usage and `days_remaining` is the fractional number of days to the next reset (`orchestrator/lib/controller.py:50-57`, `:226-227`).
If `available <= 0` the night tick skips with the reserve in its log line (`orchestrator/lib/controller.py:229-232`).
The reserve decays linearly to zero at the reset, so the week starts protective and ends fully released (`docs/design.md`, "Budget controller").
The controller keeps no memory: `available` is recomputed from measured consumption on every tick, so a heavy interactive day shrinks every later night and a quiet one grows it (`docs/design.md`, "Per-night allocation").
How that surplus is split between nights is a separate decision (inventory row 0014), and the pre-reset burn-down ignores the reserve entirely (inventory row 0026).
The P90 value was first hand-configured (`p90_daily_tokens`, then `p90_daily_usd`); it is now seeded once and scaled with the derived weekly cap (`orchestrator/lib/ratelimits.py:30`, `:233`).

## Alternatives considered

- **A fixed daily background allowance (weekly surplus divided by seven).** It ignores what the owner actually consumed, so a heavy interactive week could still be starved; reason for rejection not recorded beyond the priority rule above.
- **A static ceiling on weekly usage before a mid-week point.** This was the earlier rule: background work never pushes weekly usage above a fixed fraction of the cap before a mid-week point.
  It was replaced because the goal is to consume the whole weekly quota, and a static ceiling is counterproductive late in the week, while the decaying reserve gives the same protection dynamically (maintainer's notes).
- **A flat safety fraction of the cap as the reserve.** Rejected: the reserve should be a realistic heavy-day allowance measured from the owner's own usage, not an arbitrary percentage (maintainer's notes).
- **A reserve equal to the mean day.** Not recorded as considered; why the 90th percentile rather than another percentile is not recorded.
- **Predict the owner's remaining usage from a model of their history.** Not recorded as considered.
- **Strongest argument against.** (Own analysis.) Reserving a P90 day for every remaining day assumes every remaining day is heavy.
  At the start of the week the reserve is about seven P90 days (`orchestrator/lib/controller.py:226`), although on average fewer than one of the remaining days will be that heavy, so it holds back quota the owner will almost never use.
  The engine then depends on the per-night reallocation and the pre-reset burn-down to recover it, and anything the last night cannot absorb in its few windows is lost.
  A percentile of history is also a lagging predictor: when the owner's usage shifts (a crunch week, a new heavier model), the seeded P90 is wrong in whichever direction hurts.
  Only the per-tick re-measurement of `consumed` limits the damage.

**Would we decide the same today?** Yes - the rule is one line, needs no forecast, and fails safe for the owner, who has absolute priority; its over-reservation early in the week is recovered by the per-night reallocation of the surplus and the pre-reset burn-down (inventory rows 0014, 0026), and its input now tracks the derived weekly cap instead of a stale hand value.

## Consequences

- Good: the owner's worst plausible remaining week is protected at every point of the week, without forecasting.
- Good: stateless and deterministic in its inputs, so it is testable and every skip explains itself (`orchestrator/lib/controller.py:1-13`).
- Bad: early-week nights are small by design, and the week's useful background work concentrates late, which makes the last nights and the burn-down load-bearing.
- Bad: the protection is only as good as the P90 figure; usage from other devices is invisible to the measurement (`docs/design.md`, "Caps from rate-limit readings").

## Open questions

- Why the 90th percentile rather than another percentile, and over what history window it was first computed, is not recorded.

## Sources

- `orchestrator/lib/controller.py:1-13`, `:50-57`, `:226-232`.
- `orchestrator/lib/ratelimits.py:30`, `:233`.
- `docs/design.md`, "Quota-aware scheduling" (bullet at line 47), "Budget controller" (heading at line 131), "Per-night allocation" (heading at line 173).
- `README.md:30`.
- Commit `568f33a` (initial public release, controller already present).
- Maintainer's notes (quota strategy).
