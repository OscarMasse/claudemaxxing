# 0016. Budget in USD at list price, not in tokens; refuse token-era keys instead of converting them

- Status: accepted (retroactive)
- Date: 2026-09-12

## Context

Until 2026-09-12 the budget controller counted raw tokens: caps, reserve and session estimates were `*_tokens` keys (`0804eb5`).
A measurement that day showed the account's limit weighs tokens by price, not by count: 14M local tokens of Fable and Opus moved the weekly `/usage` bar 4 points while 22M tokens of Sonnet moved it 1, about the ratio of their list prices (`abdfdf3`, `orchestrator/lib/transcripts.py:3-9`).
A model-blind token count therefore mispaced any night that mixed models.
Consumption was already read from Claude Code's own transcripts, per model (inventory row 0015), so pricing each entry locally was possible.

## Decision

Every budget quantity is USD at Anthropic list price, computed locally from the transcripts with a per-family price table (`orchestrator/lib/transcripts.py:47-59`); an unknown model id is priced at the most expensive row and reported.
The controller math is unchanged, only the unit (`0804eb5`, `orchestrator/lib/controller.py:10-12`).
A session's actual cost is Claude Code's `total_cost_usd`, which includes subagents (`892039b`).
An account still carrying a token-era key (`weekly_cap_tokens`, `window_cap_tokens`, `p90_daily_tokens`, `est_session_tokens`, `*_min_surplus_tokens`) is reported as misconfigured and not scheduled, never converted (`orchestrator/lib/config.py:245-266`, `892039b`).
The change landed in four commits the same day: measurement (`abdfdf3`), controller and config refusal (`0804eb5`), estimator, ledger and gate (`892039b`), documentation and digest (`aec5217`).

## Alternatives considered

- **Keep tokens and weight them per model.** Not recorded as a separate option; the maintainer's notes record that an earlier attempt to give Fable its own token cap had been judged impossible, and that the missing dimension turned out to be price.
- **Convert token-era keys automatically.** Rejected: no factor turns a model-blind token count into dollars, since tokens weigh differently per model, "which is the whole point"; and the repo rule is to default a key when it is absent, never when it is present and unreadable (`0804eb5`).
- **Strongest argument against.** (Own analysis.) List-price USD is not what the subscription meters: it is a flat fee with opaque limits.
  The price weighting was calibrated from a handful of readings on one night.
  If the provider weighs cache reads, a new model or a promotion differently from list price, the engine paces confidently against the wrong unit, and every price change requires editing a hard-coded table.
  Refusing old configs also turns an upgrade into a night with no background work rather than a degraded one.

**Would we decide the same today?** Yes - the price weighting is the only one measured to reproduce the `/usage` bars, the caps are now derived from rate-limit readings in the same unit, which absorbs calibration drift (inventory row 0017), and the wall itself is observed rather than predicted (inventory row 0018), so the unit only has to pace, not to be exact.

## Consequences

- Good: mixed-model nights are paced in the unit the limit applies, and the budget matches Claude Code's own per-session `total_cost_usd`.
- Good: a stale config fails loudly with the retired key named, instead of scheduling on a meaningless number.
- Bad: the price table must be updated by hand when list prices or model families change.
- Bad: the unit is an approximation of an undisclosed metering; the documentation says explicitly that it is not what the subscription bills (`docs/design.md`, "Budget controller"; `orchestrator/config.yaml:25-32`).

## Sources

- Commits `abdfdf3`, `0804eb5`, `892039b`, `aec5217`.
- `orchestrator/lib/transcripts.py:1-9`, `:47-59`.
- `orchestrator/lib/config.py:21-25`, `:245-266`.
- `orchestrator/lib/controller.py:10-12`.
- `orchestrator/config.yaml:25-32`.
- `docs/design.md`, "Budget controller" (lines 132-134).
- Maintainer's notes (decision log).
