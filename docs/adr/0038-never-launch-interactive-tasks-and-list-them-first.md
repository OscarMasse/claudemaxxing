# 0038. Never launch `mode: interactive` tasks and list them first for the owner

- Status: accepted (retroactive)
- Date: 2026-09-28

## Context

Much of an owner's highest-priority work cannot run unattended: reviews, arbitration, decisions, conversations (maintainer's notes, `docs/design.md:52`).
Before this decision the backlog had two ways to hold such work, and both were wrong (maintainer's notes).
Marking it `blocked` misstates it: `blocked` means "waiting for an answer", so the task pollutes the owner's questions list.
Keeping it out of the backlog removes it from the only prioritised view there is, with its priority and deadline.
The decision was taken in `a22fae3` (2026-09-28); later the same day `13424b0` ordered the interactive list by deadline.

## Decision

A task may declare `mode: interactive`; the default is `mode: autonomous` (`a22fae3`, `docs/design.md:52`).
The engine never launches an interactive task, not even by name through `manual.py`, yet the task stays `ready` in the backlog with its priority and deadline (`docs/design.md:53`).
Two checks enforce it: the queue skips interactive tasks (`orchestrator/lib/tasks.py:509-511`), and `resolve`, which `manual.py` uses to launch a task by name, refuses them (`orchestrator/lib/tasks.py:587-588`).
`gate.py status` prints one `interactive task=...` line per such task (`orchestrator/gate.py:678-681`), and the digest opens with a `## Today` section listing them, as what the owner should do first in a live session (`a22fae3`, `orchestrator/prompts/digest.md`).
Tasks due within a day, or overdue, come first, earliest deadline first; the rest follow by priority (`13424b0`, `orchestrator/lib/tasks.py:671-675`).
Any other `mode:` value makes the task unschedulable and reported, never guessed (`orchestrator/lib/tasks.py:173-184`, reported by `misconfigured()` at `:872-873`).

## Alternatives considered

- **Mark the task `blocked`.** Rejected because `blocked` means waiting for an answer and the task would clutter the questions list (maintainer's notes).
- **Keep the task out of the backlog.** Rejected because it leaves the only prioritised view, losing its priority and deadline (maintainer's notes).
- **Strongest argument against.** (Own analysis.) The engine is a background scheduler, and this turns it into the owner's personal to-do list: it now orders and presents work it will never do, a second scope that grows the digest and the ordering rules.
  A task whose mode is misjudged at writing time either parks work the engine could have done or sits unnoticed if the owner skips the `## Today` section.

**Would we decide the same today?** Yes - the backlog stays the single prioritised view, the questions list stays clean, and the gatekeeper's rule is two checks (queue and by-name) that cannot launch owner work by mistake.

## Consequences

- Good: one prioritised backlog holds both background and owner work, with shared priorities, deadlines and prerequisites (unmet prerequisites are shown on the line).
- Good: the questions list only holds real questions.
- Good: an unknown `mode:` value fails loud instead of launching owner work or silently parking engine work.
- Bad: the engine does nothing with these tasks beyond listing them; whether they get done depends on the owner reading the digest.
- Bad: the interactive ordering is a second ordering rule next to the queue's, measured in days rather than nights (`orchestrator/lib/tasks.py:674-675`), to keep in step.

## Sources

- Commit `a22fae3` (introduces `mode: interactive`, the status line and the digest `## Today` section).
- Commit `13424b0` (deadline-first ordering, `due=` on the line).
- `docs/design.md:52-54` ("Interactive tasks"; this passage was in the top-level README at the time).
- `orchestrator/lib/tasks.py:168-184`, `orchestrator/lib/tasks.py:509-511`, `orchestrator/lib/tasks.py:587-588`, `orchestrator/lib/tasks.py:663-699`, `orchestrator/gate.py:677-681`, `orchestrator/prompts/digest.md`.
- Maintainer's notes (why neither `blocked` nor keeping the task out of the backlog works).
