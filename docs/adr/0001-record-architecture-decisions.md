# 0001. Record architecture decisions

- Status: accepted
- Date: 2026-09-30

## Context

The engine's design decisions are recorded today in four places that each tell part of the story.
The README's "Design details" and "FAQ" sections describe the current behaviour and some of its reasons.
Commit bodies hold most of the reasoning and the measurements behind it (for example `0386c2c`, `2d6dd86`, `6c32f0c`).
Comments in the example `orchestrator/config.yaml` explain individual knobs.
The rest lives only in the maintainer's notes, outside this repository.

None of these places records a decision as a unit: what was decided, what else was weighed and why it lost, and what the choice costs.
Some decisions were taken, reverted and replaced within days (`9ca80f2`, reverted by `2d6dd86`), so the reason a design is the way it is cannot be read from the code alone.
Most changes to this repository are written by agents, which makes an explicit, checkable record of each choice more important, not less: the owner has to be able to explain and defend every one of them.

## Decision

Architecture decisions are recorded as ADRs in `docs/adr/`, one decision per file, named `NNNN-short-title.md` and numbered in sequence.
Every ADR follows [0000-template.md](0000-template.md): Status, Date, Context, Decision, Alternatives considered (each with why it was rejected), Consequences (good and bad), and Sources.
An ADR written after the fact carries `Status: accepted (retroactive)` and the date the decision was actually taken, found in git or in the notes it is recovered from.
A rationale is never invented: when no source states why a choice was made, the ADR says so, and the question goes to the owner.
An accepted ADR is not rewritten when the decision changes; a new ADR supersedes it and the old one's status points to the new one.
[README.md](README.md) indexes the ADRs and inventories the decisions that do not have one yet.

## Alternatives considered

- **Keep the reasoning in the README.** The README describes how the engine behaves now and should stay readable as a user guide; a record of rejected alternatives and reverted designs does not belong in it, and it has no place for a decision's history.
- **Rely on commit messages.** They hold most of the reasoning today, but one decision is often spread over several commits (worktree isolation is `193b8f0`, `6bcf946`, `9671680`, `f4cd445`), and they cannot be browsed by topic.
- **A heavier template (full MADR with decision drivers, pros and cons per option).** Rejected to keep each ADR short enough to be written and read; the lean template keeps the parts needed to defend a choice.

## Consequences

- Good: every decision has one place that states its reason, its rejected alternatives and its cost, with sources that can be checked.
- Good: decisions whose reason is not recorded anywhere become visible, as explicit questions, instead of staying implicit.
- Bad: ADRs have to be kept in step with the engine; a change that reverses a decision must also add the superseding ADR.
- Bad: the retroactive ADRs are only as good as their sources; where the reasoning was never written down, they stay incomplete until the owner answers.

## Sources

- Michael Nygard, [Documenting Architecture Decisions](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions) (2011).
- [MADR](https://adr.github.io/madr/), the Markdown Architectural Decision Records template this one is trimmed from.
- README.md, sections "Design details" and "FAQ".
- Commits `9ca80f2` and `2d6dd86` (a design adopted and reverted the same day), `193b8f0`, `6bcf946`, `9671680`, `f4cd445` (one decision over four commits).
