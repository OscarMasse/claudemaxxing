# 0071. Stack linear PR-on-PR dependencies as GitHub stacked PRs; anything else waits for the merge

- Status: accepted
- Date: 2026-10-01

## Context

A `delivery: pr` task used to count as met for its dependents as soon as its status was `done`, that is once its PR was open.
A dependent then started from an `origin/main` that did not contain its prerequisite, or guessed by building on the unmerged branch; on 2026-09-25 this caused a day of cross-PR breakage (notes).
The owner also wanted to test a series of related tasks at their head and merge them in one go.

GitHub ships native stacked PRs (public preview since 2026-07-30, all repositories, docs: https://docs.github.com/en/pull-requests/get-started/about-stacked-prs).
Each layer is its own PR with its own CI and review, and the stack merges bottom-up, one prefix at a time or all at once.
A stack is a single chain of PRs in one repository: no tree, no layer with two bases.
The `gh stack` extension (`github/gh-stack`, v0.1.0 installed) offers `gh stack link`, which builds or grows a stack from branches and PRs without any local tracking state.

The decision took three steps.
On 2026-09-25 the owner chose merge-gated prerequisites and GitHub stacks over a custom shared stack branch.
On 2026-09-26 the owner fixed the rules: a failed layer holds the layers above it, a stack can mix models, partial merges are the owner's call, and a prerequisite outside the stack waits for the merge.
The first implementation (`edfed1d`, `aad8860`, `7890fbc`) declared stacks with a `stack: <name>` key.
On 2026-10-01, reviewing it before merge, the owner asked why an open-PR prerequisite should not simply stack: the key only said "do not wait for the merge", and the order of the layers already came entirely from `prerequisites:`, so two tasks with the same key and no prerequisite between them were unordered.
The key was dropped the same day, before the change was merged; this ADR records that final form.

## Decision

A prerequisite with `delivery: pr` is met only once the PR URL recorded in its notes is merged into main, checked with `gh` and failing closed (no recorded URL, or `gh` unable to answer, means unmet), unless the task stacks on it.

A task stacks on a prerequisite whose PR is still open when the dependency is a straight line (`stack_base` in `orchestrator/lib/tasks.py`):

- the task has exactly one open-PR prerequisite, its other prerequisites being met as usual;
- both tasks are `delivery: pr` and in the same project;
- that PR is open;
- no other layer sits on it yet: no open PR based on its branch other than the task's own, and no other live task depending on it that is done with an unmerged PR, in progress, or ready, launchable and earlier by name.

The launcher then branches the task's worktree from the prerequisite PR's branch instead of the default branch, in the repository the PR belongs to (`orchestrator/lib/workspace.py`), and appends a paragraph to the task directive (`orchestrator/lib/stacks.py`, `orchestrator/run.sh`).
That paragraph tells the session to rebase onto the base branch at the start of each slice, to open its PR with that branch as base (`gh pr create --base`, after the usual push by explicit HTTPS URL), and to add it on top with `gh stack link <lower PR> <its PR>`, which then pushes nothing.
Everything else waits for the merge into main: a diamond (two open-PR prerequisites), a fork (a second task on the same open PR), another project, a PR that is not open.

A stack is therefore a chain of tasks, with no key to declare; `gate.py stacks` shows each one, named after its bottom task, with its layers, CI state and a "ready to merge" verdict, then every task that waits for a merge, with the reason.
The rules of 2026-09-26 stand: a failed or blocked lower layer is not `done`, so it holds the layers above it; each layer keeps its own model and effort; the engine never merges.
The `gh stack` commands that push or edit PRs are denied to every session that is not `pr`, like the other mutating `gh` commands (`orchestrator/lib/permissions.py`).

## Alternatives considered

- **Keep `done` as enough.** The state before; the cross-PR breakage of 2026-09-25 is the recorded reason it was rejected (notes).
- **A custom shared "stack branch" managed by the engine.** Rejected by the owner on 2026-09-25: the platform now provides stacking, and the engine deletes its own mechanics when the platform provides them (notes).
- **A declared `stack: <name>` key.** Built first, then dropped on 2026-10-01 before merge: it duplicated what `prerequisites:` already says, left the order of the layers to `prerequisites:` anyway, and made the owner opt in for the common case.
- **Stack every open-PR prerequisite, diamonds and forks included.** Not possible with GitHub stacks, which are single chains in one repository.
- **Merge-gating without stacks.** It would serialise every chain of dependent tasks on the owner's merge cadence.
- **Strongest argument against.** (Own analysis.) `gh stack` is a public preview at v0.1.0: its commands or behaviour may change, and every stacked session depends on `gh stack link`.
  Automatic stacking also means a slow review of one PR holds the whole chain above it, and a change requested on a lower layer has to be carried up by a rebase in each upper layer; waiting for the merge would have kept the upper tasks off that PR entirely.
  Who takes the layer in a fork is decided by name, not by priority, and a `gh` outage stops every dependent of an open PR, since both the merge check and the stack check fail closed.

**Would we decide the same today?** Yes, pending the first real stacked night - waiting for the merge removes the recorded failure, stacking keeps a linear chain moving without the owner declaring anything, and the preview risk sits behind one directive and one helper module (own judgement).

## Consequences

- Good: a dependent never starts from a main that lacks its prerequisite, unless it is built on top of it as the next layer.
- Good: a series of related changes is reviewed and tested layer by layer, and merged in one go or by prefix, with nothing to declare beyond `prerequisites:`.
- Good: no engine-owned branch mechanics and no local stack state; GitHub re-targets the open layers when a lower one merges.
- Bad: a diamond, a fork or a cross-project dependency still waits for the owner to merge.
- Bad: a dependency on a preview CLI extension, and a few `gh` calls per tick for every task whose prerequisite has an open PR.
- Bad: a lower layer that changes after an upper one is built must be carried up by a rebase in the upper layer's next slice.

## Sources

- Commits `edfed1d` (merge-gated prerequisites), `aad8860` and `7890fbc` (the first, key-based stacks), `d9d32a8` (design docs), `cae500b` and `d830556` (`gate.py stacks`), 2026-10-01, and the commit that replaces the key with automatic stacking.
- `orchestrator/lib/tasks.py` (`stack_base`, `_unmet_prerequisites`), `orchestrator/lib/workspace.py` (`prepare`, `session_dirs`), `orchestrator/lib/stacks.py`, `orchestrator/run.sh`, `orchestrator/lib/permissions.py`.
- `docs/design.md`, "Merge-gated prerequisites" and "Automatic stacks".
- PR #41.
- Notes: the owner's decisions of 2026-09-25, 2026-09-26 and 2026-10-01, kept in the owner's task file outside this repository.
