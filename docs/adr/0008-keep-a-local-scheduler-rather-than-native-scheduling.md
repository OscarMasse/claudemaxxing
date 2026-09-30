# 0008. Keep a local scheduler rather than Claude Code's native scheduling

- Status: accepted (retroactive)
- Date: 2026-08-19

## Context

The engine spends leftover quota on a backlog while the owner sleeps (`README.md:5`), so something has to launch sessions unattended, at night, on the owner's machine.
It does so with a custom scheduler: a launchd KeepAlive job running `gatekeeper-loop.sh`, a daily digest job that doubles as a watchdog, and `pmset` wakes (`docs/design.md`, architecture diagram and "Why a KeepAlive loop instead of launchd StartInterval?"; `README.md:120-122`).
Its decisions depend on local state: quota readings from the account, owner activity, task files and per-task git worktrees on the machine (`docs/design.md`).
The maintainer's notes state a design principle: custom mechanics should be deleted once the harness offers them natively.
On 2026-08-19 the notes record a review applying that principle to scheduling, which concluded the native options could not replace the gatekeeper yet.

## Decision

Keep the local launchd-based gatekeeper as the scheduler, and do not move scheduling to Claude Code's native scheduling or to cloud scheduled agents.
The OS-specific part stays behind a documented seam of five scripts (`orchestrator/platform/README.md`), so a port swaps the adapter rather than the design.

## Alternatives considered

- **The CLI's native scheduling (CronCreate).** Rejected (maintainer's notes): it is session-bound, held in memory with a 7-day expiry, so it cannot run an unattended engine that must survive restarts.
- **Cloud scheduled agents (claude.ai routines).** Rejected (maintainer's notes): they have no access to the local filesystem, so they cannot see the quota readings, owner activity, task files or worktrees the scheduler decides from.
- **Headless runs driving themselves toward a goal.** Rejected (maintainer's notes): headless `-p` mode had no goal-style mode to replace the scheduler's loop.
- **Strongest argument against.** (Own analysis.) A custom scheduler is a permanent maintenance tax on infrastructure that is not the product: the design notes list the failures it has already cost (launchd pending spawns across DarkWake, clamshell sleep, idle detection; `docs/design.md:9`), it ties the engine to macOS, and every native scheduling improvement widens the gap between what the engine maintains and what it could get for free.

**Would we decide the same today?** Yes - as long as native scheduling stays session-bound and cloud agents cannot see the local machine, which were the facts on 2026-08-19; the project's own principle makes this a condition to re-check at each harness release, and the decision should be superseded as soon as a native, persistent, locally executing scheduler exists.

## Consequences

- Good: the scheduler sees and acts on all local state (quota, activity, worktrees) and survives reboots and sleep through launchd.
- Good: the kill switch, budgets and self-repair live in one place the engine controls (`docs/design.md`).
- Bad: the engine owns the reliability of its own scheduling, including OS quirks, and ships for macOS only (`README.md:120-122`).
- Bad: the decision rests on the current limits of the harness and has to be revisited when they change.

## Sources

- `README.md:5`, `README.md:120-122` ("macOS only, for now").
- `docs/design.md:9`, architecture diagram, "Why a KeepAlive loop instead of launchd StartInterval?".
- `orchestrator/platform/README.md` (the OS seam).
- Commit `568f33a` (initial public release with the launchd gatekeeper).
- Maintainer's notes (design principle on harness absorption; the 2026-08-19 review of native scheduling).
