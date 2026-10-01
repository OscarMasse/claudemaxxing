# 0071. Use GitHub stacked PRs for stacked work; a prerequisite outside the stack waits for the merge

- Status: accepted
- Date: 2026-09-25

## Context

A `delivery: pr` task used to count as met for its dependents as soon as its status was `done`, that is once its PR was open.
A dependent then started from an `origin/main` that did not contain its prerequisite, or guessed by building on the unmerged branch; on 2026-09-25 this caused a day of cross-PR breakage (notes).
The owner also wanted to test a series of related tasks at their head and merge them in one go.

GitHub ships native stacked PRs (public preview since 2026-07-30, all repositories, docs: https://docs.github.com/en/pull-requests/get-started/about-stacked-prs).
Each layer is its own PR with its own CI and review, `gh stack sync` rebases the layers above a changed one and force-pushes with lease, and the stack merges bottom-up, one prefix at a time or all at once.
It needs `gh` >= 2.90 and the `github/gh-stack` extension.

## Decision

Two rules, taken together on 2026-09-25 and refined with the owner on 2026-09-26 (notes):

1. A prerequisite with `delivery: pr` is met only once the PR URL recorded in its notes is merged into main, checked with `gh pr view` (`orchestrator/lib/tasks.py:325-345`, `edfed1d`).
   It fails closed: no recorded URL or an unreachable `gh` means unmet.
2. Tasks sharing a `stack: <name>` key are the layers of one GitHub stack (`docs/design.md:51`).
   Inside a stack a prerequisite only needs to be `done` (its layer pushed), not merged, since that is the point of stacking.
   The scheduler runs one layer of a stack at a time (`orchestrator/lib/tasks.py:561-582`, `aad8860`).
   Each stack session starts with `gh stack sync`, then adds its layer with `gh stack add` and `gh stack submit` (`orchestrator/prompts/orchestrate.md:35-40`, `7890fbc`).
   A failed or blocked lower layer is not `done`, so it holds every layer above it through `prerequisites:`.
   Each layer keeps its own model and effort; the stack only fixes the order.
   The engine never merges: partial merges are the owner's call, and `gate.py stacks` shows each stack as one unit in the digest, with a "ready to merge" verdict (`orchestrator/lib/stacks.py`, `cae500b`).

## Alternatives considered

- **Keep `done` as enough.** The state before; the cross-PR breakage of 2026-09-25 is the recorded reason it was rejected (notes).
- **A custom shared "stack branch" managed by the engine.** Rejected by the owner on 2026-09-25: the platform now provides stacking, and the engine deletes its own mechanics when the platform provides them (notes).
- **Merge-gating without stacks.** Not recorded as considered on its own; it would serialise every chain of dependent tasks on the owner's merge cadence, which stacks are there to avoid.
- **Strongest argument against.** (Own analysis.) `gh stack` is a public preview and the installed extension is v0.1.0: its commands or behaviour may change, and the engine's stack sessions depend on them.
  Merge-gating also makes the owner's merge cadence a hard bottleneck for every dependent outside a stack, and a `gh` outage blocks every merge-gated dependent, since the check fails closed.
  And the order inside a stack holds only through `prerequisites:` between its layers: two layers with the same `stack:` and no prerequisite between them are unordered, and nothing rejects that today.

**Would we decide the same today?** Yes, pending the first real stacked night - the rule removes the recorded failure, the preview risk sits behind one prompt paragraph and one helper module, and the owner keeps every merge (own judgement).

## Consequences

- Good: a dependent outside a stack always starts from a main that contains its prerequisite.
- Good: a series of related changes is reviewed and tested layer by layer, and merged in one go or by prefix.
- Good: no engine-owned branch mechanics; GitHub rebases and re-targets the open layers.
- Bad: dependent work outside a stack waits for the owner to merge.
- Bad: a dependency on a preview CLI extension, and one `gh pr view` per merge-gated prerequisite on every tick.
- Bad: stack order must be declared with `prerequisites:` on every layer above the bottom one.

## Sources

- Commits `edfed1d` (merge-gated prerequisites), `aad8860` (one layer of a stack at a time), `7890fbc` (stack sessions sync first), `d9d32a8` (design docs), `cae500b` and `d830556` (`gate.py stacks` in the digest), 2026-10-01.
- `orchestrator/lib/tasks.py:109-115,325-345,561-582`.
- `orchestrator/lib/stacks.py`.
- `orchestrator/prompts/orchestrate.md:35-40`.
- `docs/design.md:49-51`.
- Notes: the owner's decisions of 2026-09-25 and 2026-09-26, kept in the owner's task file outside this repository.
