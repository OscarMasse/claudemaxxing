# claudemaxxing

**Your Claude Max quota expires every week whether you use it or not. claudemaxxing spends the leftover on your own backlog while you sleep.**

You write task specs in Markdown.
Headless Claude Code sessions work them at night, inside a budget that never touches your daytime quota.
In the morning you read a digest and review pull requests.

```markdown
# Digest 2026-09-29

## To validate
- rankr PR #133 (template-from-catalog images): ready, merge and release.
- rankr PR #131 (lint rule banning raw fetch() to the API): ready, review/merge.
- claudemaxxing PR #27 (CI gate): green, merge.

## Questions
- rankr-just-dev-lan: accept the LAN image-upload limitation, or grow the task to fix it first?

## Done
- rankr-gardener (nightly duty): reviewed, opened #131, filed two follow-ups.
```

*A real morning digest, lightly trimmed. Each line is a decision the owner takes in a minute, not a log to read.*

- **Nights are budget-safe.**
  The engine reads your real consumption from Claude Code's own transcripts and keeps a reserve for your heaviest day.
  Sessions only get the surplus.
- **Mornings are yours.**
  No session opens a quota window that would cross the morning guard, and any interactive activity on the account blocks night launches.
- **Every task ends where it says.**
  `delivery: pr | branch | local` is a contract, enforced by deny rules on `git push` and `gh`, not by the prompt.
- **One kill switch.**
  `touch orchestrator/PAUSED` in the backlog stops every launch.
  Delete it to resume.
- **Runs when you say so, too.**
  `manual.sh --for 3h` burns quota at any hour, with the pacing rules off and the safety rules on.

Community project, not affiliated with Anthropic.
Runs entirely on your Mac: launchd, dependency-free Python, and the Claude Code CLI.

## Quick start

Requirements: macOS, Python 3.11+, the [Claude Code CLI](https://code.claude.com/docs/en/overview) logged into a Pro or Max account.
Nothing to install on the Python side.

Rehearse a whole night first, in a throwaway backlog with a stubbed `claude` and zero tokens:

```bash
git clone https://github.com/OscarMasse/claudemaxxing.git && cd claudemaxxing
orchestrator/e2e-sandbox.sh
```

Then point the engine at a backlog of your own.
The backlog is a plain directory: `tasks/*.md`, `config.yaml`, and the digests and state the engine writes there.

```bash
export BACKLOG_ROOT=~/backlog
mkdir -p "$BACKLOG_ROOT/tasks"
cp orchestrator/config.yaml "$BACKLOG_ROOT/config.yaml"          # edit: your accounts and projects
cp examples/tasks/research-static-site-generators.md "$BACKLOG_ROOT/tasks/"
python3 orchestrator/lib/ratelimits.py seed personal 850 58 114 2026-09-24T23:10:00+02:00   # one hand reading of /usage
orchestrator/manual.sh --tasks research-static-site-generators --dry-run
```

The dry run prints the plan and launches nothing.
When it looks right, register the nightly loop and the 07:37 digest job:

```bash
orchestrator/install.sh
```

The installer prints the one manual `pmset` step that wakes the Mac before the night starts.
Full setup, including the status line hook that keeps the quota caps calibrated, is in [docs/design.md](docs/design.md#install).

## How it works

```
  you                      the engine (every 5 min)                 Claude Code
  ----                     ------------------------                 -----------
  write tasks/*.md   -->   gatekeeper: budget, idle, window   -->   claude -p, one task per session
  read the digest    <--   digest: done / to validate / questions   <--   verify, review, push, log cost
```

1. **Capture.**
   A task is one Markdown file with frontmatter: project, priority, `delivery`, a `verification` command, a token budget.
2. **Spec.**
   A task becomes `ready` only once its definition of done is checkable.
   Spec quality is the bottleneck, not execution.
3. **Night.**
   Each tick, the gatekeeper measures what the week has consumed, computes tonight's share of the surplus, and launches as many fifty-minute sessions as it pays for.
   A session works one task in its own git worktree, runs the task's verification, then a fresh-context subagent tries to refute the work.
4. **Digest.**
   Every session journals into the day's digest as it exits.
   The morning job sorts it into done, to validate, and questions for you.

Two ideas carry the design.
Background work shares a quota with a human who must never notice it, so everything is paced from measured usage, not from a schedule.
And selection is done in pure Python before any model is launched, so a night wastes no tokens deciding what to do.

The reasoning behind each rule, the scheduling classes, the budget controller, and the FAQ live in [docs/design.md](docs/design.md).
This is the [Ralph Wiggum loop](https://ghuntley.com/ralph/) with a budget and a verifier, addressing the completion-without-testing failure mode described in Anthropic's [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents).

## Safety and limits

- **macOS only, for now.**
  Scheduling goes through launchd and `pmset`.
  The OS seam is five scripts, documented in [orchestrator/platform/README.md](orchestrator/platform/README.md); a Linux port would swap in systemd timers.
- **A closed lid sleeps.**
  Clamshell sleep has no software override.
  Lid open on AC power plus `sudo pmset -c sleep 0` is the working setup.
- **Per-session cost cap.**
  Every session runs with `--max-budget-usd` (`max_session_usd`, about five times the observed maximum), so a runaway session dies instead of draining the week.
- **Quota exhaustion is observed, not predicted.**
  The budget paces the week from local estimates.
  Hitting a model's limit is recorded per model, and that model is not offered again until the stated reset.
- **What an agent may push.**
  A `pr` task may push its branch and open a pull request.
  A `branch` task commits locally.
  A `local` task leaves nothing outside the machine.
  Force pushes, `git filter-branch` and credential reads are denied to every session, and no session ever pushes to `main`.
- **Where an agent may write.**
  Only its own worktree under `<repo>/.agent-worktrees/<task>`.
  The main checkout, and your uncommitted work in it, is never touched.
- **What it cannot see.**
  Usage from other devices or from claude.ai is invisible to the transcript scan, so the caps are recalibrated from the status line's rate-limit readings rather than trusted once.

## Checks

`./check.sh` runs every gate: config parsing, `ruff`, `shellcheck`, and the unit tests.
CI runs the same script on every pull request.

## License

MIT, see [LICENSE](LICENSE).
