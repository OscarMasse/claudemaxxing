# Claudemaxxing

Your Claude subscription resets every week whether you used it or not.
Claudemaxxing puts that idle quota to work: it drives headless Claude Code sessions against a personal task backlog while you sleep, spending only the capacity your interactive use would never touch.
Runs entirely locally on macOS: launchd, a few hundred lines of dependency-free Python, and the Claude Code CLI.
Community project, not affiliated with Anthropic.

This engine has been running my own backlog nightly since August 2026.
First month: 172 commits from background sessions, 44 task specs, 15 tasks completed fully autonomously (spec to verified done), 16 morning digests.
The failure modes were all infrastructure (launchd pended spawns, clamshell sleep, mtime-based idle detection), not model quality; each fix is documented where it lives.

## How it works

```
launchd (KeepAlive)                          launchd (07:37 daily)
        |                                            |
        v                                            v
gatekeeper-loop.sh  --every 30 min-->  gatekeeper.sh   digest-wrapper.sh
                                            |          (watchdog: kickstarts
                                            v           the loop if dead)
                                        gate.py tick         |
                                            |                |
              +-----------------------------+                |
              |  per account: ccusage quota data, that       |
              |  profile's transcript activity, cost ledger, |
              |  RUNNING locks; plus config.yaml, task        |
              |  frontmatter, PAUSED                         |
              |  prints, per account: SKIP <account> <why>   |
              |  or RUN <account> <slice> <task> <model>     |
              |  <effort> <project> (one line per slot)      |
              +-----------------------------+                |
                                            v                v
                                run.sh --account <name> <slice> [task] ...
                                            |
                                            v
                              headless `claude -p` session
                              (account profile, project dirs,
                               slice timeout, cost cap,
                               task work + adversarial review)
                                            |
                                            v
                              state/<account>/costs.jsonl (ledger)
                              state/runs.log    (history)
                              tasks/*.md        (status, notes)
                              NEEDS-HUMAN.md    (questions)
                              digests/<day>.md  (live run journal)
```

One constraint drives the whole design: background work shares a quota with a human who must never notice it.

Prior art: this is the [Ralph Wiggum loop](https://ghuntley.com/ralph/) (Geoffrey Huntley) with a budget and a verifier - same "one task per fresh-context session against a backlog" core, plus quota-aware scheduling and mandatory verification, which addresses the completion-without-testing failure mode Anthropic documents in [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents).

- **Verification before landing.** Every task declares a `verification` method (tests, script, checkable criteria, optionally a per-item `## Acceptance` checklist), and each session must pass an adversarial review: a fresh-context subagent instructed to refute the work, looped until a pass finds zero new major issues.
- **Quota-aware scheduling.** A decaying reserve protects a P90 heavy day for every remaining day of the quota week; background sessions consume only the surplus, mostly at night (02:00-06:00).
- **Activity lock.** Idle time comes from interactive event timestamps in session transcripts; any recent human activity on an account blocks its background launches.
- **Prerequisite gating.** `prerequisites: <task> <task>` in a task's frontmatter keeps it unscheduled until every named task is `done`.
- **Kill switch.** `touch orchestrator/PAUSED` stops all launches; deleting it resumes.
- **Live morning digest.** Every run journals its progress into the day's digest file as it finishes; the 07:37 session curates it into "done autonomously" vs "needs the human". Readable at any hour.

## Accounts and projects

An **account** is one Claude subscription: its own profile dir, quota calibration, budget, activity lock, and cost ledger.
A **project** routes tasks to an account and declares which directories its sessions may write.
The tool imposes no taxonomy; task frontmatter picks a project with `project: <name>`.

The motivating example: work tasks run on an employer-provided subscription, personal tasks on your own.
Neither budget, idle clock, nor ledger ever crosses over, so your employer's quota never subsidizes your hobby projects (or the reverse).
A legacy flat config with no `accounts`/`projects` sections still works: a `default` account and project are synthesized.

## Install

Requirements: **macOS only** (the shipped scheduling adapter is launchd + pmset; the seam for other OSes is documented in `orchestrator/platform/README.md`), Python 3.11+, the Claude Code CLI, Node (for `npx ccusage`).
Nothing to install on the Python side: standard library only, no virtualenv, no pip.

1. Clone the repo; its root is the backlog root (`tasks/`, `digests/`, `NEEDS-HUMAN.md` live there, gitignored).
2. Declare your `accounts` and `projects` in `orchestrator/config.yaml` (fully documented example in the file).
3. Calibrate each account's token numbers: compare `/usage` against `npx ccusage blocks --json` for a few days.
4. Create a task in `tasks/` (see `examples/tasks/`) with `status: ready` and a matching `project:`.
5. Run `orchestrator/install.sh`; it registers the gatekeeper loop and the daily digest job, and prints the one manual `pmset` step for nightly wake.
6. Optional: a fine-grained PAT in `~/.config/backlog-agents/github-token` enables background git pushes.

Manual trigger: `orchestrator/run.sh <minutes> [--account <name>]`.
Dry run: `dry_run: true` in the config, then watch `state/gatekeeper.log` for a night.

To run the engine from this checkout against a separate backlog, set `BACKLOG_ROOT` when installing (`BACKLOG_ROOT=~/backlog orchestrator/install.sh`) and put your real config at `$BACKLOG_ROOT/config.yaml`; it lives outside this repo and is never committed.

Note: a closed MacBook lid cannot stay awake for the night regime (clamshell sleep has no software override); lid open on AC power plus `sudo pmset -c sleep 0` is the working setup.

## Design details

**Scheduling regimes, per account.**
Night (02:00-06:00): up to `night_parallel` slots, stronger model floors allowed, guarded so no 5h quota window crosses the morning guard into the workday.
Day surplus: one sonnet slot, armed only when the remaining nights cannot absorb the surplus and the current quota window is not serving the owner.
Pre-reset burn-down: the last hours before the weekly reset spend the expiring surplus, upgrading to the strongest model the doomed surplus justifies.

**Budget controller.**
`available = weekly_cap - consumed - p90_daily_reserve * days_remaining`; the reserve decays linearly to zero at reset, so the week starts protective and ends fully released.
Slot counts are budget-driven: a session's estimated burn must fit the slice budget.

**The ledger is the memory.**
Every session's result JSON is appended to `state/<account>/costs.jsonl`; measured burn rates per (task, model) feed the next scheduling decision, and the digest surfaces per-task cost so the owner can kill money pits.

## FAQ

**Does it run on Linux or Windows?**
Not yet, but the engine is portable: everything OS-specific sits behind the seam in `orchestrator/platform/` (five hooks, documented in its README).
A Linux port would swap launchd for systemd timers in `platform/linux/`.

**Why a KeepAlive loop instead of launchd StartInterval?**
This launchd domain left scheduled spawns pending across DarkWake cycles ("pended nondemand spawn"), losing whole nights.
A KeepAlive loop ticks itself; launchd only has to restart it if it dies, and the daily digest job doubles as a watchdog that kickstarts it.

**Why hard budget caps per session?**
`--max-budget-usd` is runaway protection, not pacing: a budget kill is a hard stop with no resume point.
The cap sits ~5x the observed max per-run cost, so it only fires on a genuinely broken session.

**Why does the gatekeeper pick tasks, not the session?**
The model must be known before launch (`claude -p --model ...`), and selection in pure Python costs zero tokens.
Everything decidable locally is decided locally.

**Why derive idle time from transcript events instead of file mtimes?**
Two independent writers bump transcript mtimes with no human present (external tools rewriting session files, Claude Code appending housekeeping events).
Only `user`/`assistant` event timestamps count; anything else starves the night regime.

## License

MIT, see [LICENSE](LICENSE).
