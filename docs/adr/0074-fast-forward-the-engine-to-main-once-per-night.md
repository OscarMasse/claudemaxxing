# 0074. Fast-forward the engine to main once per night, validated first

- Status: accepted
- Date: 2026-10-04

## Context

The engine runs straight from its main checkout under launchd, and nothing updated it.
On 2026-10-04 that checkout was 6 commits behind main, so merged PRs were not running.
Setting a new config key would have crashed every tick: the old `lib/config.py` refuses unknown project keys.

## Decision

At the first tick inside a night window with no fresh RUNNING lock on any account, `gate.py selfupdate` tries once (a marker file per night, so one night runs on one engine version).
`lib/selfupdate.py` fetches `origin/main` and fast-forwards only.
It refuses, keeping the current SHA and journalling why, when the checkout is not on `main`, is dirty (untracked `.serena/`, `.claude/`, `.agent-worktrees/` excepted), has diverged or is ahead, the fetch fails, or the candidate's own `lib/config.py` cannot load the live `config.toml` (validated from a scratch export before switching).
It never resets, stashes or forces.
The old and new SHA and the merged commit subjects go to `gatekeeper.log` and to the digest `## Runs`.
bash reads a running script lazily, so after an update `gatekeeper-loop.sh` does `exec /bin/bash "$SELF"` instead of looping on stale text; a test pins it.

## Alternatives considered

- **Update on every tick.** Rejected: the digest could not attribute a night to one version.
- **Hard reset to main.** Rejected: destroys local work.
- **Run the full `check.sh` before switching.** Deferred: config load is the failure that actually happened.

## Consequences

- Good: merged engine changes reach the night without a manual pull.
- Bad: a config key needs the engine that knows it, so a new key takes effect the night after its engine PR is merged.
- A refusal is only visible in the digest; a persistently dirty checkout means a stale engine.

## Sources

- `orchestrator/lib/selfupdate.py`, `orchestrator/gate.py` (`selfupdate_cmd`), backlog task `claudemaxxing-engine-self-update`.
