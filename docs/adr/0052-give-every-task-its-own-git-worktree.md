# 0052. Give every task its own git worktree, never a repo's main checkout

- Status: accepted (retroactive)
- Date: 2026-09-28

## Context

Background sessions used to work in whatever directory their project listed, which for a code project is the repo's main checkout.
That checkout is shared: the owner keeps uncommitted work there, and several sessions can run at once.
An incident is recorded in `orchestrator/lib/workspace.py:1-12`: a session ran `git checkout -b <branch> origin/main` in a main checkout while another session had uncommitted work there, and the switch carried that work onto the new branch; a `git stash` or `git reset --hard` at the wrong moment would have destroyed it.
The same docstring names the root cause: the choice of a worktree had been left to each session's judgement, and that is what failed.
Sessions also started in the engine's own main checkout, so a git command that forgot its `-C` acted on a real checkout (`orchestrator/lib/workspace.py:77-84`, `6bcf946`).

The decision was taken and built in four commits on 2026-09-28: `193b8f0` (worktree per task, launch refused without one, janitor, prompt rules), `6bcf946` (review fixes: branch from `origin/HEAD`, a failed preparation blocks the task, sessions start outside every repo, project-level `workdir: main`), `9671680` (reuse of a task's declared `branch:`), `f4cd445` (a declared branch that exists only on the remote is taken from it).
It shipped in PR #35, verified over two real nights with no session touching a main checkout, and landed on `origin/main` at `5f34d26` on 2026-09-30.

## Decision

The launcher, not the session, decides where a session writes.
Before launching, `run.sh` asks `orchestrator/lib/workspace.py` for the session's directories, and every project dir that is the top level of a git repo comes back as `<repo>/.agent-worktrees/<task>` on branch `agent/<task>`, created from the remote's default branch or reused by the next slice of the same task (`docs/design.md`, "One worktree per task").
Only that worktree is passed as `--add-dir`; a session whose worktree cannot be prepared is not launched, and its task is set `blocked` with the reason and a line for the owner (`6bcf946`).
A task whose work predates this names its existing branch with `branch:`; the launcher reuses the worktree holding it, attaches it at the task path, takes it from the remote if it only exists there, and refuses a branch checked out in a main checkout (`9671680`, `f4cd445`, `orchestrator/lib/workspace.py:175-185`).
A task or a project that must work in the main checkout declares `workdir: main` (for example a project whose repo is a notes store rather than code); any other value makes the task unschedulable and is reported (`orchestrator/lib/tasks.py:156-189`, `193b8f0`, `6bcf946`).
Sessions start in a directory outside every repo, so a git command without `-C` fails instead of switching a checkout (`orchestrator/lib/workspace.py:77`).
The prompt forbids `git checkout`, `git switch`, `git stash`, `git reset --hard` and `git clean` outside the session's own worktree (`orchestrator/prompts/orchestrate.md:20-31`).
Removing finished worktrees is left to the night janitor, recorded separately as decision 0054.

## Alternatives considered

- **Leave the choice of a worktree to the session, guided by the prompt.** This was the state before, and the incident above is the recorded reason it was rejected (`orchestrator/lib/workspace.py:9-12`).
- **Start sessions in the engine's main checkout.** The first version kept this; the review in `6bcf946` moved sessions outside every repo, since a missing `-C` would otherwise act on a real checkout.
- **A fresh worktree per slice instead of per task.** Reason not recorded beyond the design: deterministic per-task names are the record, so nothing else has to remember which worktree belongs to which task, and a later slice picks up the previous one's work (`orchestrator/lib/workspace.py:14-20`).
- **Strongest argument against.** (Own analysis.) Worktrees multiply cost per task: each one is a full checkout on disk, and each needs its own dependency install and, for a project with a dev stack, its own compose stack and ports, which is why a stack janitor had to follow.
  Isolation is also partial: a worktree shares the repo's object store, refs, stash stack and hooks with the main checkout and every other worktree, so a session's `git stash` or a hook still reaches shared state.
  And the main-checkout ban for ordinary dirs is prompt-only: `--add-dir` and the start directory make a mistake unlikely, but no deny rule stops `git -C <main checkout> checkout`; mutating git verbs are denied only on read-only optional dirs (`orchestrator/lib/permissions.py:103-108`).
  Keeping one checkout per project, with sessions serialised on it, would avoid all three costs at the price of concurrency.

**Would we decide the same today?** Yes - the recorded failure was a session damaging shared uncommitted work, and a per-task worktree chosen by the launcher removes that failure for the cost of disk and setup time, which two verified nights showed to be acceptable (PR #35); the prompt-only gap is narrowed by the start directory outside every repo.

## Consequences

- Good: the owner's and other sessions' uncommitted work in a main checkout is out of a session's reach by default.
- Good: a task's work survives across slices on a known branch at a known path, with no bookkeeping.
- Good: a failed preparation blocks the task visibly instead of silently falling back to the main checkout.
- Bad: disk use, dependency installs and dev stacks grow with the number of open tasks, and finished worktrees must be reaped (decision 0054).
- Bad: git state shared across worktrees (stash stack, hooks, refs) is still shared, and the ban on touching a main checkout is enforced by prompt and layout, not by deny rules.
- Bad: `workdir: main` is an escape hatch that restores the old risk for the tasks and projects that declare it.

## Sources

- Commits `193b8f0`, `6bcf946`, `9671680`, `f4cd445` (2026-09-28).
- PR #35 "orchestrator: every background session works in its own worktree", merged 2026-09-30, head `5f34d26` on `origin/main`.
- `orchestrator/lib/workspace.py:1-58` (rationale, layout, cleanup), `:77` (`session_cwd`), `:175-185` (declared branch).
- `orchestrator/lib/tasks.py:156-189` (`workdir` validation).
- `orchestrator/lib/permissions.py:103-108` (mutating git verbs denied only on read-only optional dirs).
- `orchestrator/prompts/orchestrate.md:20-31`.
- `docs/design.md`, "One worktree per task"; `README.md:138-139`.
