# Architecture decision records

This directory records the architecture decisions of the engine: the gatekeeper, the budget and pacing, model routing, the task format, the delivery rails, worktree isolation, verification and reporting.
The format and the rules are set by [0001](0001-record-architecture-decisions.md); new ADRs start from [0000-template.md](0000-template.md).

## Index

| No. | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted |

## Inventory

Every decision below is a candidate ADR that has not been written yet.
The number is the one the ADR will take.

- **Date**: the day the decision was taken, from the commit that introduced it.
  A decision taken before the initial public release (`568f33a`, 2026-09-04) is dated by month from the maintainer's notes.
- **Sources**: where the decision and its reasons can be read.
  `README.md:N` is a line of the top-level README at the commit that adds this inventory; a short SHA is a commit of this repository.
  `notes` means part of the reasoning is only in the maintainer's design notes, outside this repository, and will be restated in the ADR.
- **Rationale**: `yes` when the sources state why and what was rejected, `partial` when they state why but not the alternatives or the costs, `missing` when no source states why.
  Every `missing` row is a question for the owner, listed under [Open questions](#open-questions).

### Architecture and scheduling core

| No. | Decision | Date | Sources | Rationale |
|---|---|---|---|---|
| 0002 | Keep the backlog as plain Markdown task files with YAML-like frontmatter | 2026-08 | `README.md:41-46`, `examples/tasks/`, `568f33a`, notes | partial |
| 0003 | Select tasks in a local, zero-token gatekeeper, never in the session | 2026-08 | `README.md:230-232`, notes | yes |
| 0004 | One task per session (a multi-task budgeted session was adopted and reverted) | 2026-09-08 | `9ca80f2`, `2d6dd86`, `b90f4fe` | yes |
| 0005 | Run the night as a pipeline: a 5-minute tick that never waits on the sessions it launched | 2026-09-08 | `b90f4fe`, `README.md:217-220` | yes |
| 0006 | Tick from a KeepAlive loop instead of launchd StartInterval; the daily digest job doubles as watchdog | 2026-08 | `README.md:222-224`, `orchestrator/platform/README.md:9,12`, notes | yes |
| 0007 | Fresh context for every slice, resumed from notes in the task file, not from `claude -p --resume` | 2026-08 | `orchestrator/prompts/orchestrate.md:111`, notes | yes |
| 0008 | Keep a local custom scheduler rather than Claude Code's native scheduling | 2026-08 | `README.md:5,51`, notes | yes |
| 0009 | Standard-library Python only, with a hand-rolled YAML subset for the config | 2026-08 | `README.md:5,87`, `orchestrator/config.yaml:1-10` | missing |
| 0010 | Keep OS specifics behind a platform seam of five hooks; ship the macOS adapter only | 2026-08 | `orchestrator/platform/README.md`, `README.md:86,213-215` | partial |
| 0011 | Resolve the backlog root explicitly and fail loudly instead of falling back to the example data | 2026-09-28 | `02ec85e`, `README.md:107-110`, notes | yes |
| 0012 | Several accounts and projects, each account with its own budget, idle clock and ledger | 2026-08 | `568f33a`, `README.md:74-82`, `orchestrator/config.yaml:121-126` | yes |

### Quota, budget and cost

| No. | Decision | Date | Sources | Rationale |
|---|---|---|---|---|
| 0013 | Pace the week with a decaying reserve of one heavy (P90) day per remaining day | 2026-08 | `README.md:54,166`, notes | yes |
| 0014 | Back-load each night's share of the weekly surplus; the last night absorbs the whole burn-down | 2026-09-07 | `7924fe2`, `00d9735`, `b90f4fe`, `README.md:179-183`, `orchestrator/config.yaml:93-101` | yes |
| 0015 | Measure consumption from Claude Code's own transcripts instead of ccusage | 2026-09-09 | `0386c2c`, `README.md:169` | yes |
| 0016 | Budget in USD at list price, not in tokens; token-era keys are refused, not converted | 2026-09-12 | `abdfdf3`, `0804eb5`, `892039b`, `aec5217`, `README.md:137-140` | yes |
| 0017 | Derive the weekly and window caps from status-line rate-limit readings instead of configuring them | 2026-09-26 | `28d672a`, `93a9d6e`, `README.md:141-164` | partial |
| 0018 | Observe quota exhaustion per model family from the limit message instead of predicting it | 2026-09-09 | `0386c2c`, `README.md:171-175` | yes |
| 0019 | Learn a session's cost per (task, model) as the max of its recent sessions, with a p75 cold-start prior | 2026-09-08 | `f638845`, `892039b`, `README.md:206-208`, `orchestrator/config.yaml:102-108` | yes |
| 0020 | Append every session's result to a cost ledger that feeds the next decision and the digest | 2026-08 | `README.md:203-204`, notes | yes |
| 0021 | A hard per-session cost ceiling (`--max-budget-usd`) as runaway protection, not as pacing | 2026-08 | `README.md:226-228`, `orchestrator/config.yaml:115-119`, notes | yes |

### Regimes and slices

| No. | Decision | Date | Sources | Rationale |
|---|---|---|---|---|
| 0022 | Only two automatic regimes, night and pre-reset burn-down; daytime runs are manual only | 2026-09-07 | `a4da83f`, `README.md:135` | yes |
| 0023 | A missed night is not caught up the next day | 2026-09-08 | `README.md:183`, notes | yes |
| 0024 | No background quota window may cross the morning guard, so the last cold start is the guard minus 5h | 2026-08 | `README.md:124-125`, notes | yes |
| 0025 | End of window: a guard tolerance and a minimum slice, and no relaunch ladder into shorter slices | 2026-09-27 | `ce9870d`, `ab6eebc`, `README.md:126-128`, `orchestrator/config.yaml:50-53,69-72`, notes | yes |
| 0026 | The pre-reset burn-down runs with no budget, no reserve and no activity lock | 2026-09-11 | `6c32f0c`, `6d08203`, `README.md:55,132-134` | yes |
| 0027 | Detect owner activity from interactive transcript events, not from file mtimes | 2026-08 | `README.md:55,234-236`, `c2a633c`, notes | yes |
| 0028 | Give each session its absolute start and deadline, and tag early exits | 2026-09-27 | `ce9870d`, `README.md:129`, notes | yes |

### Models and slots

| No. | Decision | Date | Sources | Rationale |
|---|---|---|---|---|
| 0029 | A task's `model:` is what its session runs: no ceiling, no upgrade, no ordering between models | 2026-09-11 | `6c32f0c`, `b90f4fe`, `README.md:130-131` | yes |
| 0030 | Serialize Fable with `max_fable_slots`, counted over running sessions and applied before the queue is cut | 2026-09-09 | `0386c2c`, `f01b3cc`, `2fb483d`, `README.md:168,176-177`, `orchestrator/config.yaml:78-91` | yes |
| 0031 | `max_parallel_sessions` is a machine safety ceiling; the budget decides how many sessions run | 2026-09-07 | `7924fe2`, `README.md:167`, `orchestrator/config.yaml:73-77` | yes |

### Queue and task lifecycle

| No. | Decision | Date | Sources | Rationale |
|---|---|---|---|---|
| 0032 | Order the queue by Kanban classes of service: task priority first, project rank only breaks ties | 2026-09-27 | `aa6ed38`, `README.md:185-191` | yes |
| 0033 | Priority and deadlines propagate to prerequisites, with backward scheduling from the deadline | 2026-09-09 | `355b1a4`, `aa6ed38`, `e75bebd`, `README.md:192-194` | yes |
| 0034 | Prerequisites are declared in frontmatter and enforced by the scheduler, not read from prose | 2026-08 | `README.md:56`, `orchestrator/prompts/orchestrate.md:88-97` | partial |
| 0035 | Recurring work as duties (nightly or weekly, no daily) and fillers (at most once per night) | 2026-09-07 | `7924fe2`, `1a01555`, `ee49082`, `ee5b0a0`, `README.md:196-200` | yes |
| 0036 | Work that needs no reasoning runs as a plain scheduled job, not as a session | 2026-09-07 | `7924fe2`, `README.md:201` | partial |
| 0037 | Defer a ready task with `not_before` rather than blocking it | 2026-09-29 | `be5db55`, `bfe0148`, `README.md:57`, notes | yes |
| 0038 | `mode: interactive` tasks are never launched and are listed first for the owner | 2026-09-28 | `a22fae3`, `13424b0`, `README.md:59-62` | partial |
| 0039 | Done tasks archive themselves once idle for the lock TTL; lookups by name resolve the archive | 2026-09-28 | `2c12e4f`, `acfabe5`, `e0d8a94`, `README.md:58` | partial |
| 0040 | The gatekeeper claims a task at launch instead of leaving the claim to the session | 2026-09-12 | `902654e`, `README.md:205` | yes |
| 0041 | Reset stale `in-progress` tasks to `ready`, keeping their notes | 2026-09-08 | `b90f4fe`, `README.md:67` | yes |
| 0042 | No silent defaults: only an absent key gets a default; an unreadable value makes the task unschedulable and reported | 2026-09-08 | `b90f4fe`, `4ca8cb2`, `0804eb5`, `README.md:66` | yes |
| 0043 | Block stalled or storming tasks, and pause the engine on a launch-failure storm | 2026-09-25 | `3efc395`, `1829642`, `3b00625`, `7fb2812`, notes | yes |
| 0044 | Kill switch: a `PAUSED` file stops every launch | 2026-08 | `568f33a`, `README.md:65`, notes | partial |
| 0045 | Warn a session nearing its `token_budget` from a PreToolUse hook in orchestrator-owned settings | 2026-09-25 | `2b161eb`, `75bccec`, `orchestrator/hooks/token_budget.py`, notes | yes |

### Delivery, autonomy and rails

| No. | Decision | Date | Sources | Rationale |
|---|---|---|---|---|
| 0046 | Autonomy levels `private`, `visible`, `gated`: `gated` actions are never performed unattended | 2026-08 | `examples/tasks/`, `orchestrator/prompts/orchestrate.md:42,72-76`, notes | partial |
| 0047 | Every task declares its delivery (`branch`, `pr`, `local`) as an obligation, on an axis separate from autonomy | 2026-09-09 | `c8c25bc`, `README.md:63` | yes |
| 0048 | Enforce the delivery rails with per-session deny rules built in one place, failing closed | 2026-09-15 | `095b3d3`, `515cade`, `32a5a02`, `README.md:64`, `orchestrator/lib/permissions.py`, notes | yes |
| 0049 | Give the agent's GitHub token only to sessions that may publish, and push by explicit HTTPS URL | 2026-09-12 | `e306420`, `000ee13`, `a8320b8` | yes |
| 0050 | Remove API base URL and key variables from the environment of every background session | 2026-09-25 | `35646f5`, notes | partial |
| 0051 | Pull requests stay draft while work remains on the task | 2026-09-27 | `69d6cef`, `orchestrator/prompts/orchestrate.md:53-57` | partial |

### Workspace isolation

| No. | Decision | Date | Sources | Rationale |
|---|---|---|---|---|
| 0052 | One git worktree per task, never a repo's main checkout; sessions start outside every repo | 2026-09-28 | `193b8f0`, `6bcf946`, `9671680`, `f4cd445`, `README.md:68,70` | yes |
| 0053 | Optional dirs are read-only unless a task declares them in `uses:` | 2026-09-28 | `fa258d4`, `README.md:69`, `orchestrator/config.yaml:161-167` | yes |
| 0054 | Night janitors remove finished worktrees and abandoned compose stacks, only when nothing is running | 2026-09-24 | `b95a48a`, `193b8f0`, `6bcf946`, `README.md:70-71` | yes |
| 0055 | Headless sessions run subagents and Bash in the foreground only | 2026-09-25 | `282404f`, `orchestrator/prompts/orchestrate.md:155-159` | yes |

### Verification and reporting

| No. | Decision | Date | Sources | Rationale |
|---|---|---|---|---|
| 0056 | Mandatory verification, then an adversarial fresh-context review with a capped number of passes, before `done` | 2026-08 | `README.md:51,53`, `orchestrator/prompts/orchestrate.md:131-136`, notes | yes |
| 0057 | A live digest: every run journals into the day's file, curated once each morning | 2026-08 | `README.md:72`, `orchestrator/prompts/digest.md`, `orchestrator/config.yaml:54-58` | partial |
| 0058 | Questions for the owner go to `NEEDS-HUMAN.md`, with a clickable notification only while the owner is at the machine | 2026-08 | `0386c2c`, `README.md:96-98`, `orchestrator/platform/README.md:13` | partial |
| 0059 | Manual launches skip every pacing rule and keep every safety rule | 2026-09-26 | `e36bc9d`, `README.md:100-102`, notes | yes |

### Tooling

| No. | Decision | Date | Sources | Rationale |
|---|---|---|---|---|
| 0060 | Rehearse the engine offline: an end-to-end sandbox with a stubbed `claude`, and `dry_run` | 2026-09-07 | `d5ae75c`, `README.md:104-105` | yes |
| 0061 | CI runs exactly the local `check.sh`; a change is done only once CI is green | 2026-09-29 | `39cb0f6`, `README.md:114-119`, `check.sh`, notes | yes |
| 0062 | Config spellings: flow-style lists and `config.yaml`, with the older forms still parsed | 2026-09-06 | `07d8e6e` | yes |

## Open questions

Questions for the owner, from the `missing` rows and from inconsistencies found while building the inventory.

- **0009.** Why standard-library Python only, and a hand-rolled YAML subset rather than a YAML library? Was it to avoid a virtualenv under launchd, to keep installation to a clone, or something else?
- **0038.** What made interactive work a task mode of its own, rather than a `blocked` task or a task left out of the backlog?
- **0056.** `README.md:53` says the adversarial review is "looped until a pass finds zero new major issues", while `orchestrator/prompts/orchestrate.md:135` caps it at 3 passes. Which one is the decision? The ADR will record it and the other text should follow.
