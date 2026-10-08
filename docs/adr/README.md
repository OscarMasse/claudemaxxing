# Architecture decision records

This directory records the architecture decisions of the engine: the gatekeeper, the budget and pacing, model routing, the task format, the delivery rails, worktree isolation, verification and reporting.
The format and the rules are set by [0001](0001-record-architecture-decisions.md); new ADRs start from [0000-template.md](0000-template.md).

## Index

| No. | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted |
| [0007](0007-resume-each-slice-from-task-notes-in-a-fresh-context.md) | Resume each slice from task notes in a fresh context | accepted (retroactive) |
| [0008](0008-keep-a-local-scheduler-rather-than-native-scheduling.md) | Keep a local scheduler rather than Claude Code's native scheduling | accepted (retroactive) |
| [0009](0009-use-standard-library-python-only.md) | Use standard-library Python only, with a hand-rolled YAML subset for the config | accepted (retroactive) |
| [0013](0013-pace-the-week-with-a-decaying-p90-reserve.md) | Pace the week with a decaying reserve of one heavy (P90) day per remaining day | accepted (retroactive) |
| [0016](0016-budget-in-usd-at-list-price.md) | Budget in USD at list price, not in tokens; refuse token-era keys instead of converting them | accepted (retroactive) |
| [0038](0038-never-launch-interactive-tasks-and-list-them-first.md) | Never launch `mode: interactive` tasks and list them first for the owner | accepted (retroactive) |
| [0052](0052-give-every-task-its-own-git-worktree.md) | Give every task its own git worktree, never a repo's main checkout | accepted (retroactive) |
| [0068](0068-run-headless-sessions-in-bypass-mode-with-deny-rules-as-the-limits.md) | Run headless sessions in bypass mode, with deny rules as the only enforced limits | accepted (retroactive) |
| [0071](0071-use-github-stacked-prs-for-stacked-work.md) | Stack linear PR-on-PR dependencies as GitHub stacked PRs; anything else waits for the merge | accepted |
| [0072](0072-pace-on-the-live-usage-percentage.md) | Pace on the live `/usage` percentage; the derived cap is only a fallback | accepted |
| [0073](0073-weight-the-reserve-by-workday.md) | Weight the daily reserve by workday; off days keep a share | accepted |
| [0074](0074-tell-waiting-on-a-task-from-blocked-on-the-owner.md) | Tell waiting on a task from blocked on the owner | accepted |
| [0075](0075-fast-forward-the-engine-to-main-once-per-night.md) | Fast-forward the engine to main once per night, validated first | accepted |

## Inventory

This table is the decision log: every architecture decision of the engine has a row, whether or not it has an ADR.
The number is the one its ADR takes, when it has one.

- **Date**: the day the decision was actually taken: the commit that introduced it, or an earlier day when the maintainer's notes record the decision before it was committed.
  A row that merges several steps of one decision gives the date of each step.
  A step is a commit or a note that changes what the decision says; a fix or a hardening that leaves it unchanged is not a step.
  An ADR carries a single date: when a row with several steps is written up, the ADR takes the date of the step it records as the decision, and its Context tells the other steps with their dates.
  A decision taken before the initial public release (`568f33a`, 2026-09-04) is dated here by month on purpose, because its exact day is only in the maintainer's notes, or `<= 2026-09-04` when it is already in that release and no source dates it earlier.
  Its ADR takes the day the notes give, or for a `<= 2026-09-04` row the release day, the earliest the public history can show.
- **Sources**: where the decision and its reasons can be read.
  `README.md:N` is a line of the top-level README at the commit that adds this inventory; a short SHA is a commit of this repository.
  `notes` means part of the reasoning, or the date, is only in the maintainer's design notes, outside this repository, and will be restated in the ADR.
- **ADR**: the ADR that records the decision, `in NNNN` when the row is folded into another ADR, or `log only` when the row is the decision's whole record (see [Which rows get an ADR](#which-rows-get-an-adr)).
- **Rationale**: `yes` when the sources state why and what was rejected, `partial` when they state why but not the alternatives or the costs, `missing` when no source states why.
  No row is `missing` today; a new one would be a question for the owner.

### Architecture and scheduling core

| No. | Decision | Date | Sources | Rationale | ADR |
|---|---|---|---|---|---|
| 0002 | Keep the backlog as plain Markdown task files with YAML-like frontmatter | 2026-08 | `README.md:44,78,93`, `examples/tasks/`, `568f33a`, notes | partial | log only |
| 0003 | Select the task of every scheduled launch in a local, zero-token gatekeeper, not in the session | 2026-08 | `README.md:230-232`, notes | yes | log only |
| 0004 | One task per session (a multi-task budgeted session was adopted and reverted) | 2026-08, reaffirmed 2026-09-08 | `README.md:51`, `9ca80f2`, `2d6dd86`, `b90f4fe`, notes | yes | log only |
| 0005 | Run the night as a pipeline: a 5-minute tick that never waits on the sessions it launched | 2026-09-08 | `b90f4fe`, `README.md:217-220` | yes | log only |
| 0006 | Tick from a KeepAlive loop instead of launchd StartInterval; the daily digest job doubles as watchdog | 2026-08 | `README.md:222-224`, `orchestrator/platform/README.md:9,12`, notes | yes | log only |
| 0007 | Fresh context for every slice, resumed from notes in the task file, not from `claude -p --resume` | 2026-08 | `orchestrator/prompts/orchestrate.md:111`, notes | yes | [0007](0007-resume-each-slice-from-task-notes-in-a-fresh-context.md) |
| 0008 | Keep a local custom scheduler rather than Claude Code's native scheduling | 2026-08 | `README.md:5,51`, notes | yes | [0008](0008-keep-a-local-scheduler-rather-than-native-scheduling.md) |
| 0009 | Standard-library Python only, with a hand-rolled YAML subset for the config | 2026-08 | `README.md:5,87`, `orchestrator/config.yaml:1-10`, notes | yes (answered 2026-09-30, config format to be superseded by TOML, PR #36) | [0009](0009-use-standard-library-python-only.md) |
| 0010 | Keep OS specifics behind a platform seam of five hooks; ship the macOS adapter only | <= 2026-09-04 | `568f33a`, `orchestrator/platform/README.md`, `README.md:86,94,102,213-215` | partial | log only |
| 0011 | Resolve the backlog root explicitly and fail loudly instead of falling back to the example data | 2026-09-27 | `02ec85e`, `README.md:107-110`, notes | yes | log only |
| 0012 | Several accounts and projects, each account with its own budget, idle clock and ledger | <= 2026-09-04 | `568f33a`, `README.md:74-82`, `orchestrator/config.yaml:121-126` | partial | log only |
| 0066 | Session slots are `RUNNING.N` lock files, taken as soon as the account is resolved and broken after a lock TTL | 2026-08, before any config read 2026-09-08 | `orchestrator/run.sh:61-98`, `orchestrator/gate.py:42`, `b90f4fe`, `f01b3cc`, `README.md:177`, notes | partial | log only |
| 0067 | Every tick logs its decision and the reason, one line per account or slot | 2026-08 | `README.md:28-30,104`, `3d0739e`, notes | partial | log only |
| 0068 | Headless sessions run with `--permission-mode bypassPermissions`; deny rules are the only enforced limits | <= 2026-09-04 | `568f33a`, `orchestrator/run.sh:199,271`, `orchestrator/lib/permissions.py:1-5` | yes (answered 2026-09-30) | [0068](0068-run-headless-sessions-in-bypass-mode-with-deny-rules-as-the-limits.md) |

### Quota, budget and cost

| No. | Decision | Date | Sources | Rationale | ADR |
|---|---|---|---|---|---|
| 0013 | Pace the week with a decaying reserve of one heavy (P90) day per remaining day | 2026-08 | `README.md:54,166`, notes | partial | [0013](0013-pace-the-week-with-a-decaying-p90-reserve.md) |
| 0014 | Weight each night's share of the weekly surplus by a back-loading ratio, flat by default; the last night absorbs the whole burn-down | 2026-09-07, flat default 2026-09-08 | `7924fe2`, `00d9735`, `b90f4fe`, `README.md:179-183`, `orchestrator/config.yaml:93-101` | yes | log only |
| 0015 | Measure consumption from Claude Code's own transcripts instead of ccusage | 2026-09-09 | `0386c2c`, `README.md:169` | yes | log only |
| 0016 | Budget in USD at list price, not in tokens; token-era keys are refused, not converted | 2026-09-12 | `abdfdf3`, `0804eb5`, `892039b`, `aec5217`, `README.md:137-140` | yes | [0016](0016-budget-in-usd-at-list-price.md) |
| 0017 | Derive the weekly and window caps from status-line rate-limit readings instead of configuring them; headless sessions record the same readings from their stream-json `rate_limit_event` lines, tagged by origin | 2026-09-25, headless 2026-10-01 | `28d672a`, `93a9d6e`, `README.md:141-164`, `orchestrator/lib/ratelimits.py`, notes | yes | log only |
| 0018 | Observe quota exhaustion per model family from the limit message instead of predicting it; a `rejected` `rate_limit_event` is a second, structured signal | 2026-09-09, structured 2026-10-01 | `0386c2c`, `README.md:171-175`, `orchestrator/lib/ratelimits.py` | yes | log only |
| 0072 | Pace on the live `used_percentage` (reading, then extrapolated with the engine spend since) within a staleness bound; ledger USD over the derived cap only as fallback; every figure labelled with its source | 2026-10-03 | `orchestrator/lib/ratelimits.py` (`direct`), `orchestrator/gate.py` (`snapshot`), notes | yes | [0072](0072-pace-on-the-live-usage-percentage.md) |
| 0073 | Reserve weighted per local day: 1.0 on `workdays`, `offday_reserve_ratio` (default 0.25) otherwise; no `workdays` = calendar days | 2026-10-05 | `orchestrator/lib/controller.py` (`reserve`), backlog task | yes | [0073](0073-weight-the-reserve-by-workday.md) |
| 0019 | Learn a session's cost per (task, model) as the max of its recent sessions, with a p75 cold-start prior | 2026-09-08, max 2026-09-12 | `f638845`, `892039b`, `README.md:206-208`, `orchestrator/config.yaml:102-108` | yes | log only |
| 0020 | Append every session's result to a cost ledger that feeds the next decision and the digest | 2026-08 | `README.md:203-204`, notes | partial | log only |
| 0021 | A hard per-session cost ceiling (`--max-budget-usd`) as runaway protection, not as pacing | 2026-08 | `README.md:226-228`, `orchestrator/config.yaml:115-119`, notes | yes | log only |

### Regimes and slices

| No. | Decision | Date | Sources | Rationale | ADR |
|---|---|---|---|---|---|
| 0022 | Only two automatic regimes, night and pre-reset burn-down; daytime runs are manual only | 2026-09-07 | `a4da83f`, `README.md:135` | yes | log only |
| 0023 | A missed night is not caught up the next day | 2026-09-08 | `README.md:183`, notes | yes | log only |
| 0024 | No background quota window may cross the morning guard, so the last cold start is the guard minus 5h | 2026-08 | `README.md:124-125`, notes | yes | log only |
| 0025 | End of window: a guard tolerance and a minimum slice, and no relaunch ladder into shorter slices | 2026-09-27 | `ce9870d`, `ab6eebc`, `README.md:126-128`, `orchestrator/config.yaml:50-53,69-72`, notes | yes | log only |
| 0026 | The pre-reset burn-down runs with no budget, no reserve and no activity lock | 2026-09-11, lock dropped 2026-09-24 | `6c32f0c`, `6d08203`, `README.md:55,132-134` | yes | log only |
| 0027 | Detect owner activity from interactive transcript events, not from file mtimes | 2026-08 | `README.md:55,234-236`, `c2a633c`, notes | yes | log only |
| 0069 | Recent owner activity on an account blocks that account's night launches (activity lock) | 2026-08 | `README.md:55`, `orchestrator/config.yaml:63-66`, notes | partial | log only |
| 0028 | Give each session its absolute start and deadline, and tag early exits | 2026-09-27 | `ce9870d`, `README.md:129`, notes | yes | log only |
| 0063 | Work in time-boxed slices re-decided every tick, not one long run; a hard kill at the slice plus 10 minutes | 2026-08 | `README.md:33-39`, `orchestrator/run.sh:240,268`, `orchestrator/lib/with_timeout.py`, notes | partial | log only |

### Models and slots

| No. | Decision | Date | Sources | Rationale | ADR |
|---|---|---|---|---|---|
| 0029 | A task's `model:` (and `effort:`, one of low/medium/high/xhigh/max, default medium since 2026-10-07) is what its session runs, with a default when absent: no ceiling, no upgrade, no ordering between models | no night ceiling 2026-09-08, task's model 2026-09-11 | `6c32f0c`, `b90f4fe`, `README.md:130-131`, `orchestrator/config.yaml:113-114` | yes | log only |
| 0030 | Serialize Fable with `max_fable_slots`, counted over running sessions and applied before the queue is cut | 2026-09-09, running count and pre-cut filter 2026-09-12 | `0386c2c`, `f01b3cc`, `2fb483d`, `README.md:168,176-177`, `orchestrator/config.yaml:78-91` | yes | log only |
| 0031 | `max_parallel_sessions` is a machine safety ceiling; the budget decides how many sessions run | 2026-08, one machine ceiling 2026-09-07 | `7924fe2`, `README.md:167`, `orchestrator/config.yaml:73-77`, notes | yes | log only |

### Queue and task lifecycle

| No. | Decision | Date | Sources | Rationale | ADR |
|---|---|---|---|---|---|
| 0032 | Order the queue by Kanban classes of service: task priority first, project rank only breaks ties | 2026-09-27 | `aa6ed38`, `README.md:185-191` | yes | log only |
| 0033 | Priority and deadlines propagate to prerequisites, with backward scheduling from the deadline | 2026-09-09, deadlines 2026-09-27 | `355b1a4`, `aa6ed38`, `e75bebd`, `README.md:192-194` | yes | log only |
| 0034 | Prerequisites are declared in frontmatter and enforced by the scheduler, not read from prose | <= 2026-09-04 | `README.md:56`, `orchestrator/prompts/orchestrate.md:88-97` | partial | log only |
| 0035 | Recurring work as duties (nightly or weekly, no daily) and fillers (at most once per night) | 2026-09-07, filler once per night 2026-09-25 | `7924fe2`, `1a01555`, `ee49082`, `ee5b0a0`, `README.md:196-200` | yes | log only |
| 0036 | Work that needs no reasoning runs as a plain scheduled job, not as a session | 2026-09-07 | `7924fe2`, `README.md:201` | partial | log only |
| 0037 | Defer a ready task with `not_before` rather than blocking it | 2026-09-29 | `be5db55`, `bfe0148`, `README.md:57`, notes | yes | log only |
| 0038 | `mode: interactive` tasks are never launched and are listed first for the owner | 2026-09-28 | `a22fae3`, `13424b0`, `README.md:59-62`, notes | yes (answered 2026-09-30) | [0038](0038-never-launch-interactive-tasks-and-list-them-first.md) |
| 0039 | Done tasks archive themselves once idle for the lock TTL; lookups by name resolve the archive | 2026-09-28 | `2c12e4f`, `acfabe5`, `e0d8a94`, `README.md:58` | partial | log only |
| 0040 | The gatekeeper claims a task at launch instead of leaving the claim to the session | 2026-09-12 | `902654e`, `README.md:205` | yes | log only |
| 0041 | Reset stale `in-progress` tasks to `ready`, keeping their notes | 2026-09-08 | `b90f4fe`, `README.md:67` | yes | log only |
| 0042 | No silent defaults: only an absent key gets a default; an unreadable value makes the task unschedulable and reported | orphaned tasks reported 2026-09-06, general rule 2026-09-08 | `b90f4fe`, `4ca8cb2`, `0804eb5`, `README.md:66` | yes | log only |
| 0043 | Block stalled or storming tasks, and pause the engine on a launch-failure storm | 2026-09-17 | `3efc395`, `1829642`, `3b00625`, `7fb2812`, notes | yes | log only |
| 0044 | Kill switch: a `PAUSED` file stops every launch | 2026-08 | `568f33a`, `README.md:65`, notes | partial | log only |
| 0045 | Warn a session nearing its `token_budget` from a PreToolUse hook in orchestrator-owned settings | 2026-09-16 | `2b161eb`, `75bccec`, `orchestrator/hooks/token_budget.py`, notes | yes | log only |
| 0064 | A `parallel: true` task may get several sessions at once, which shard its work; it is never claimed | 2026-08, never claimed 2026-09-12 | `README.md:205`, `902654e`, `3b00625`, notes | partial | log only |
| 0070 | A `delivery: pr` prerequisite is met only once its PR is merged into main, checked with `gh` and failing closed | 2026-09-25 | `edfed1d`, `docs/design.md:50`, `orchestrator/lib/tasks.py:325-345`, notes | yes | in [0071](0071-use-github-stacked-prs-for-stacked-work.md) |
| 0071 | A linear PR-on-PR dependency stacks automatically as a GitHub stacked PR; diamonds, forks and cross-project prerequisites wait for the merge; no `stack:` key, the engine never merges | 2026-09-25, rules 2026-09-26, automatic 2026-10-01 | `edfed1d`, `docs/design.md:50-51`, `orchestrator/lib/tasks.py` (`stack_base`), notes | yes | [0071](0071-use-github-stacked-prs-for-stacked-work.md) |

### Delivery, autonomy and rails

| No. | Decision | Date | Sources | Rationale | ADR |
|---|---|---|---|---|---|
| 0046 | Autonomy levels `private`, `visible`, `gated`: `gated` actions are never performed unattended | 2026-08 | `examples/tasks/`, `orchestrator/prompts/orchestrate.md:42,72-76`, notes | partial | log only |
| 0047 | Every task declares its delivery (`branch`, `pr`, `local`) as an obligation, on an axis separate from autonomy | 2026-09-09 | `c8c25bc`, `README.md:63` | yes | log only |
| 0048 | Enforce the delivery rails with per-session deny rules built in one place, failing closed | 2026-09-15, one builder and failing closed 2026-09-25 | `095b3d3`, `515cade`, `32a5a02`, `README.md:64`, `orchestrator/lib/permissions.py`, notes | yes | in [0068](0068-run-headless-sessions-in-bypass-mode-with-deny-rules-as-the-limits.md) |
| 0049 | Push by explicit HTTPS URL, and tell the session so in its prompt | 2026-09-12, workflow files 2026-09-27 | `e306420`, `a8320b8` | yes | log only |
| 0050 | Remove API base URL and key variables from the environment of every background session | 2026-09-25 | `35646f5`, notes | partial | log only |
| 0051 | Pull requests stay draft while work remains on the task | 2026-09-27 | `69d6cef`, `orchestrator/prompts/orchestrate.md:53-57` | partial | log only |
| 0065 | Hand the agent's GitHub token only to sessions of projects that may publish | 2026-09-24 | `000ee13` | partial | log only |

### Workspace isolation

| No. | Decision | Date | Sources | Rationale | ADR |
|---|---|---|---|---|---|
| 0052 | One git worktree per task, never a repo's main checkout; sessions start outside every repo | 2026-09-28 | `193b8f0`, `6bcf946`, `9671680`, `f4cd445`, `README.md:68,70` | yes | [0052](0052-give-every-task-its-own-git-worktree.md) |
| 0053 | Optional dirs are read-only unless a task declares them in `uses:` | 2026-09-28 | `fa258d4`, `README.md:69`, `orchestrator/config.yaml:161-167` | partial | log only |
| 0054 | Night janitors remove finished worktrees and abandoned compose stacks, only when nothing is running | 2026-09-24, worktrees 2026-09-28 | `b95a48a`, `193b8f0`, `6bcf946`, `README.md:70-71` | yes | log only |
| 0055 | Headless sessions run subagents and Bash in the foreground only | 2026-09-25 | `282404f`, `orchestrator/prompts/orchestrate.md:155-159` | yes | log only |

### Verification and reporting

| No. | Decision | Date | Sources | Rationale | ADR |
|---|---|---|---|---|---|
| 0056 | Mandatory verification, then an adversarial fresh-context review with a capped number of passes, before `done` | 2026-08 | `README.md:51,53`, `orchestrator/prompts/orchestrate.md:131-136`, notes | yes | log only |
| 0057 | A live digest: every run journals into the day's file, curated once each morning | 2026-08, live journal <= 2026-09-04 | `README.md:72`, `orchestrator/prompts/digest.md`, `orchestrator/config.yaml:54-58`, `568f33a`, notes | partial | log only |
| 0058 | Questions for the owner go to `NEEDS-HUMAN.md`, with a clickable notification only while the owner is at the machine | 2026-08, clickable 2026-09-09 | `0386c2c`, `README.md:96-98`, `orchestrator/platform/README.md:13`, notes | partial | log only |
| 0059 | Manual launches skip every pacing rule and keep every safety rule | 2026-09-26 | `e36bc9d`, `README.md:100-102`, notes | yes | log only |

### Tooling

| No. | Decision | Date | Sources | Rationale | ADR |
|---|---|---|---|---|---|
| 0060 | Rehearse the engine without spending quota: `dry_run`, then an end-to-end sandbox with a stubbed `claude` | 2026-08, sandbox 2026-09-07 | `568f33a`, `d5ae75c`, `README.md:104-105`, notes | partial | log only |
| 0061 | CI runs exactly the local `check.sh`; a change is done only once CI is green | 2026-09-28 | `39cb0f6`, `README.md:114-119`, `check.sh`, notes | partial | log only |
| 0062 | Config compatibility: flow-style lists and `config.yaml` preferred; older spellings and the legacy flat config still parse | <= 2026-09-04, spellings 2026-09-06 | `07d8e6e`, `README.md:82`, `orchestrator/config.yaml:17-22` | partial | log only |

## Which rows get an ADR

Seventy rows for an engine of a few thousand lines is a decision log, not seventy ADRs.
The table above IS the log and stays complete.
An ADR file is written only for the decisions that shape the system and that a reader could reasonably contest; every other row is `log only`, its line in the table being its whole record.

Selected for an ADR (the backfill may argue for adding or dropping one, in its PR):

- 0001 record decisions; 0008 a local scheduler rather than the CLI's native scheduling
- 0007 fresh context per slice, resumed from notes
- 0009 standard-library only, and the config format
- 0013 pacing with a decaying P90 reserve; 0016 budgets in USD
- 0038 `mode: interactive`
- 0068 bypass permissions with deny rules, and the delivery rails they enforce
- 0052 one worktree per task; 0071 GitHub stacks for stacked work
- 0048, the delivery rails, is folded into 0068 rather than written separately: the rails are what makes bypass mode acceptable
- 0070, merge-gated prerequisites, is folded into 0071: automatic stacks are what makes waiting for the merge affordable

Each ADR written must carry the strongest argument against the decision and a "Would we decide the same today?" line.
When the answer is no, the ADR points to the follow-up that supersedes it.

## Documentation drift found while building the inventory

These texts contradict a decision above; the ADR, once written, is the reference they should follow.

- `README.md:53` says the adversarial review is "looped until a pass finds zero new major issues", while `orchestrator/prompts/orchestrate.md:135`, the authority for the review procedure, caps it at 3 passes (0056).
- `orchestrator/config.yaml:84-85` says the pre-reset burn-down "upgrades sessions to Fable", which `6c32f0c` removed: nothing upgrades a task's model (0029).
- `README.md:89` says the repo root is the backlog root, while `02ec85e` made an unset backlog root an error unless `ORCH_EXAMPLE=1` selects the example data (0011).
- `README.md:24` ("token usage") and `README.md:124` ("tonight's token allocation") still speak of tokens, while the budget unit is USD (0016).
- `README.md:181` says the night share "is back-loaded", while the default `night_budget_ratio: 1.0` (`orchestrator/config.yaml:101`) is a flat split (0014).
