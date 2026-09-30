# 0068. Run headless sessions in bypass mode, with deny rules as the only enforced limits

- Status: accepted (retroactive)
- Date: 2026-09-04

## Context

Every orchestrated session runs `claude -p` headless, with nobody at the terminal to answer a permission prompt.
The first unanswered prompt freezes the session until the slice's lock expires, so the slice is lost (maintainer's notes).
The initial public release already launched sessions with `--permission-mode bypassPermissions` (`568f33a`, `orchestrator/run.sh:136` at that commit), as a stated judgment call of the first runner (maintainer's notes).
There was never an allow list.

At first, autonomy was bounded only by the task frontmatter (its `delivery:` key) and the rules written in the session prompt.
Experience showed that prose alone does not hold the rails that must never break, so they moved into deny rules in three steps:

- 2026-09-15: `run.sh` passes `--disallowedTools "Bash(git push*)"` (and its `rtk` variant) to every session whose delivery is not `pr`, because in bypass mode the prompt's "do NOT push" was the only thing between a `branch` or `local` task and origin (`095b3d3`).
- 2026-09-25: the rules move to a single builder, `orchestrator/lib/permissions.py`, one family per function with its reason: pushes and mutating `gh` calls for every non-`pr` session, force pushes, `filter-branch` and credential reads for every session (`515cade`).
- 2026-09-25: `run.sh` fails closed, refusing to launch a session when the builder errors or prints nothing, and more `gh` writes are covered (`32a5a02`).

A later step reused the same builder to make undeclared optional dirs read-only (`fa258d4`).
This ADR also covers inventory row 0048 ("Enforce the delivery rails with per-session deny rules built in one place, failing closed"), folded in here because the rails are what makes bypass mode acceptable.

## Decision

Headless sessions run with `--permission-mode bypassPermissions`, and the only enforced limits are per-session `--disallowedTools` deny rules.
`orchestrator/lib/permissions.py` builds those rules in one place from the task's delivery and read-only dirs, and `run.sh` does not launch a session if the builder fails or returns nothing (`orchestrator/run.sh:199-212`, `orchestrator/run.sh:271`).
A deny rule is evaluated by the harness before the model has a say and costs no tokens (`orchestrator/lib/permissions.py:1-5`).
Beyond the rules, trust rests on isolation: a worktree per session, a scoped token, one account per session, rather than on the prompt (maintainer's notes).

## Alternatives considered

- **An allow list of tools and commands.** An allow list would have to predict every tool and command a development task may need, and every omission becomes a blocking prompt that freezes the slice; in practice it ends up allowing everything (maintainer's notes).
- **Rails in the prompt and task frontmatter only.** This was the starting point, and it was dropped because a session that forgets or misreads the prose can still push or merge; a deny rule cannot be talked around (`095b3d3`, maintainer's notes).
- **Deny rules assembled inline in `run.sh`.** This was the first step (`095b3d3`); it was replaced by one tested builder as the families grew (`515cade`, `orchestrator/tests/test_permissions.py`), exact reason for the move not recorded beyond the commit's structure.
- **A hook script checking each command.** Not adopted; the builder's docstring notes that deny rules need no hook script (`orchestrator/lib/permissions.py:3-5`), no further comparison recorded.
- **Strongest argument against.** In bypass mode any command not denied runs, and deny rules are globs over the command string: a session that spells a command differently (a script, an alias, `python -c`, a variable path) walks past them, as the builder itself lists among its accepted gaps (`orchestrator/lib/permissions.py:7-15`, `orchestrator/lib/permissions.py:80-82`, `orchestrator/lib/permissions.py:123`).
  The real blast radius is therefore bounded by isolation (worktree, scoped token, single account), not by the rules, and an isolation gap would be exposed directly.

**Would we decide the same today?** Yes - a headless session cannot answer prompts, an allow list degenerates into allowing everything, and deny rules plus isolation stop the realistic failure (a session forgetting the prose) at no token cost.

## Consequences

- Good: sessions never block on a prompt, so a slice is never lost waiting for an answer nobody will give.
- Good: the rails that must hold (no push or GitHub write outside `pr`, no force push, no credential read) are enforced by the harness, not by the model's judgment.
- Good: one builder with tests holds every family and its reason, and a broken builder stops the launch instead of running a session without rails.
- Bad: the rules stop a session that forgot the rules, not one that circumvents them; every new command spelling has to be enumerated by hand.
- Bad: some rules over-match (for example `git -C * ` prefixes and `gh api` body flags), accepted because a false refusal costs less than a missed push (`orchestrator/lib/permissions.py:41-43`, `orchestrator/lib/permissions.py:83-84`).
- To watch: isolation (worktree per session, scoped token, account boundary) is the real safety net and must stay intact.

## Sources

- `568f33a` (initial release, `orchestrator/run.sh:136` there: `--permission-mode bypassPermissions`).
- `095b3d3` (per-delivery push deny, 2026-09-15).
- `515cade` (single deny-rule builder, 2026-09-25).
- `32a5a02` (fail closed, more `gh` writes, 2026-09-25).
- `fa258d4` (read-only optional dirs through the same builder).
- `orchestrator/run.sh:199-212`, `orchestrator/run.sh:271`.
- `orchestrator/lib/permissions.py`, `orchestrator/tests/test_permissions.py`.
- `docs/design.md` "Rails as deny rules".
- Maintainer's notes (the judgment call behind bypass mode, the rejection of an allow list, isolation as the basis of trust).
