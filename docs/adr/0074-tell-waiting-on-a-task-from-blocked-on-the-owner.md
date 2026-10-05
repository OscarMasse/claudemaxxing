# 0074. Tell waiting on a task from blocked on the owner

- Status: accepted
- Date: 2026-10-05

## Context

`gate.py status` printed a `ready` task with unmet prerequisites as `blocked task=... unmet=...`, the same word as an owner question (`status: blocked`).
On 2026-10-05 an interactive session read `rankr-seed-review-pokemon` as waiting on the owner because of that overlap.
A task that needs the owner only after other tasks finish (`rankr-seed-publish-prod`) is legitimately `blocked` with `prerequisites`, but reports listed its question while it was not askable yet.
The owner asked for "a distinction between blocked because I have to step in, and blocked because it waits on another task".

## Decision

Two words, never mixed.
`blocked` is a stored status meaning the task waits on the owner.
`waiting` is a label derived from unmet prerequisites, whatever the task's status; it is never written to frontmatter.
`gate.py status` prints `waiting task=<name> on=<prereqs>` for every task with an unmet prerequisite, and `blocked task=<name>` only for a `blocked` task whose prerequisites are all met.
The digest lists only the latter under Questions, the former on one `Waiting on tasks` line.
A session that waits only on another task sets `ready` and adds it to `prerequisites` (orchestrate prompt step 6).

## Alternatives considered

- **A stored `waiting` status.** Rejected: it would go stale the moment its prerequisite lands, and every session would have to maintain it.
- **A lint for `blocked` tasks without a question.** Rejected: whether the Notes hold a question cannot be detected reliably.

## Consequences

- Good: an owner question surfaces exactly when it becomes answerable, and status output no longer reads as asking the owner about night work.
- Bad: consumers of the old `blocked task=... unmet=...` line (the backlog skills) had to be updated.

## Sources

- `orchestrator/lib/tasks.py` `waiting`, `owner_blocked`; `orchestrator/gate.py` status.
- Backlog task `claudemaxxing-waiting-vs-blocked`.
