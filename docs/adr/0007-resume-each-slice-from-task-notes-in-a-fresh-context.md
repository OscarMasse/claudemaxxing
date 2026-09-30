# 0007. Resume each slice from task notes in a fresh context

- Status: accepted (retroactive)
- Date: 2026-08-19

## Context

A task often needs more than one slice to finish, so every session must be able to pick up where the previous one stopped.
The engine is built on the "one task per fresh-context session against a backlog" core of the Ralph Wiggum loop (`docs/design.md`, section on prior art).
The Claude Code CLI offers another way to continue work: `claude -p --resume <session_id>` reopens a previous session with its full context.
The maintainer's notes record that this option was weighed and rejected on 2026-08-19, during a review of which custom mechanics the harness could absorb.
The notes-based resume was already in place at the public release (`568f33a`, 2026-09-04).

## Decision

Every slice starts a new session with a fresh context, and the only state carried between slices is the resume point written in the task file.
At the end of a slice the session writes, in the task's `## Notes`, what is done, what is next and the exact commands and files, then hands the task back as `ready` (`orchestrator/prompts/orchestrate.md:111`).
The next session, possibly days later, reads that note and resumes from it.
A session killed mid-task keeps its last notes, so the gatekeeper's repair resumes rather than restarts it (`docs/design.md`, "Self-repair").

## Alternatives considered

- **Resume the previous session with `claude -p --resume <session_id>`.** Rejected (maintainer's notes): a fresh context per slice avoids context rot, and the compressed state in the task notes stays readable by the owner and by the digest, unlike an opaque session context.
  The notes add that the question should not be reopened unless headless, budgeted goal-style runs appear in the CLI.
- **Strongest argument against.** (Own analysis.) Every slice pays again to re-read the repository, the task and its notes before doing useful work, which on a quota-bound engine is spent tokens that produce nothing new.
  The written resume point is also a lossy summary: whatever tacit understanding the previous session had built (why an approach failed, which file was subtly wrong) survives only if that session thought to write it down, and a session cut off by its slice or its budget may write a poor note or none.

**Would we decide the same today?** Yes - the task file stays the one state any session, the digest or the owner can read, and a resumed context would reintroduce both context rot and an opaque state; the re-check condition recorded in the notes (headless budgeted goal-style runs) is the trigger to revisit.

## Consequences

- Good: each slice starts clean, with no accumulated context degrading its work.
- Good: the resume state is plain Markdown in the task file, readable by the owner, the digest and any other tool or agent, and it survives a crash, a killed session or a change of model.
- Bad: every slice re-reads the task and the repository, which costs tokens.
- Bad: continuity is only as good as the note the previous session wrote; tacit context that was not written down is lost.

## Sources

- `orchestrator/prompts/orchestrate.md:111` (step 7, the resume point in `## Notes`).
- `docs/design.md`, prior art paragraph and "Self-repair".
- Commit `568f33a` (initial public release, notes-based resume present).
- Maintainer's notes (the 2026-08-19 harness-absorption review, rejected `--resume`).
