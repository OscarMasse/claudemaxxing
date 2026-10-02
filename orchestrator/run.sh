#!/bin/bash
# Launch one background orchestrator session (or the morning digest).
# Usage: run.sh [--account NAME] <slice_min> [task_file] [model] [effort] [project]
#                                [est_usd] [delivery]
#        run.sh --digest [--account NAME]
# The account may also come from the ORCH_ACCOUNT env var; without either, the
# first account in config.toml is used. The account selects the Claude profile
# (CLAUDE_CONFIG_DIR), the binary, and the state/<account>/ namespace.
# Running this directly is the single-session MANUAL trigger (manual.sh runs
# batches): it bypasses the gatekeeper's quota locks (but not the RUNNING
# lock) - you decide, it runs.
# Without a task_file the session picks the task itself (sonnet only), among
# the projects of this account.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
ORCH_DIR="$(pwd)"
# launchd hands down a bare PATH (/usr/bin:/bin:...), so node and the homebrew
# tools are missing and anything the session shells out to that needs them (a
# hook, an MCP server, a project's test command) dies with FileNotFoundError.
# Same export as gatekeeper.sh. Set before the first python3 call: it must
# be Homebrew's 3.14, not the macOS system 3.9.
export PATH="${ORCH_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin}"
# The backlog root (tasks/, digests/, NEEDS-HUMAN.md, orchestrator/state/) is
# resolved by lib/config.py (env, then the file install.sh records) and
# exported so gate.py resolves the same config and state paths. It exits
# non-zero with a message naming both when neither says where the backlog is.
BACKLOG_ROOT="$(python3 lib/config.py backlog-root)" || exit 2
export BACKLOG_ROOT
STATE_ROOT="$BACKLOG_ROOT/orchestrator/state"

ACCOUNT="${ORCH_ACCOUNT:-}"
MODE="orchestrate"
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --account) ACCOUNT="$2"; shift 2 ;;
    --digest)  MODE="digest"; shift ;;
    *)         ARGS+=("$1"); shift ;;
  esac
done
SLICE_MIN="${ARGS[0]:-15}"
TASK_FILE="${ARGS[1]:-}"; MODEL_OVR="${ARGS[2]:-}"; EFFORT_OVR="${ARGS[3]:-}"
PROJECT="${ARGS[4]:-}"
# What the gatekeeper predicted this session would cost, in USD at list price
# (the budget unit since 2026-09-12). Recorded in the ledger next to the
# session's actual cost_usd so the estimate can be scored (ledger.accuracy).
EST_USD="${ARGS[5]:-0}"
# The task's declared delivery contract (branch|pr|local), passed down so the
# session is told its obligation instead of re-deriving it - and so a slice can
# never push work the task did not ask to be pushed. Empty only when no task
# was pre-selected; the session then reads the key from the task it picks.
DELIVERY="${ARGS[6]:-}"
if [ "$MODE" = "digest" ]; then SLICE_MIN=15; TASK_FILE=""; PROJECT=""; DELIVERY=""; fi

# All config access goes through lib/config.py (accounts inherit flat keys).
# The live config file is resolved once, in the single place that owns the
# order: ORCH_CONFIG, then $BACKLOG_ROOT/config.toml, then the repo default.
CONFIG_FILE="$(python3 lib/config.py resolve)" || exit 2
cfg() { python3 lib/config.py "$CONFIG_FILE" "$@"; }
if [ -z "$ACCOUNT" ]; then ACCOUNT="$(cfg first-account)"; fi

# Slot-based locks, namespaced per account: up to 8 concurrent sessions (the
# gatekeeper caps how many get launched per regime and per account; manual
# runs take a slot like any other). Accounts never contend for slots.
#
# Claimed HERE, as the first thing this script does after resolving its
# account, and deliberately ahead of every other config read. The lock is what
# makes two launches of the same slot impossible, so every millisecond between
# process start and lock acquisition is a window in which a second launcher
# can pass the same check. The profile, dirs and digest lookups below are one
# python3 subprocess each - about a second in total, which is a wide enough
# window to matter once the gatekeeper ticks every few minutes.
#
# The lock records `<pid> <epoch> <model>`. The model is the gatekeeper's
# argument (ARGS[2]), known before the lock and before any config read, so it
# can be written here; it is empty for the digest and for manual runs that
# leave it to the config. gate.py reads it to count the Fable sessions that
# are RUNNING, not just the ones a single tick launches (2026-09-12: a second
# Fable session was launched next to one five minutes old).
STATE="$STATE_ROOT/$ACCOUNT"
mkdir -p "$STATE"
SLOT=""
for i in 1 2 3 4 5 6 7 8; do
  if ( set -o noclobber; echo "$$ $(date +%s) ${MODEL_OVR:-}" > "$STATE/RUNNING.$i" ) 2>/dev/null; then
    SLOT=$i; break
  fi
done
if [ -z "$SLOT" ]; then echo "no free slot account=$ACCOUNT" >> "$STATE_ROOT/runs.log"; exit 0; fi
LOCK="$STATE/RUNNING.$SLOT"
trap 'rm -f "$LOCK"' EXIT INT TERM


# The account's Claude profile drives the invocation AND where usage /
# activity detection read, so each subscription is fully self-contained.
CLAUDE_CONFIG_DIR="$(cfg account "$ACCOUNT" claude_config_dir)"
export CLAUDE_CONFIG_DIR
# Background sessions run on the subscription only: a base URL or API key
# inherited from the launching shell would silently bill a per-token account
# instead. Nothing sets them today; this keeps it that way.
unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
CLAUDE_BIN="$(cfg account "$ACCOUNT" claude_bin)"
MODEL="${MODEL_OVR:-$(cfg account "$ACCOUNT" claude_model)}"
EFFORT="${EFFORT_OVR:-$(cfg account "$ACCOUNT" claude_effort)}"
MAX_USD="$(cfg account "$ACCOUNT" max_session_usd 15)"

# GitHub credentials for background sessions: a fine-grained PAT scoped to the
# repos the agents may touch (Contents + Pull requests only). GH_TOKEN drives
# `gh` (pr create etc.); GIT_ASKPASS answers git's HTTPS prompts. Push targets
# are HTTPS URLs; protect main with pre-push hooks or branch protection.
# Only for a known project that may publish: a `local_only_default` project
# (the employer's repos) never pushes, and the PAT cannot see its org either -
# exporting it there made every read-only `gh` call return an empty list
# instead of falling back to the owner's keyring login (2026-09-17). Digest
# and auto-pick sessions carry no project and never push, so they get none.
AGENT_GH_TOKEN_FILE="$HOME/.config/backlog-agents/github-token"
if [ -n "$PROJECT" ] && [ "$(cfg project-local-only "$PROJECT")" != "true" ] \
   && [ -f "$AGENT_GH_TOKEN_FILE" ]; then
  GH_TOKEN="$(cat "$AGENT_GH_TOKEN_FILE")"
  export GH_TOKEN
  export GIT_ASKPASS="$HOME/.config/backlog-agents/git-askpass.sh"
fi

# Directories the session may write to (--add-dir): the picked task's project
# dirs, or every project dir of this account when no task was pre-selected.
# The backlog root itself is always included.
if [ -n "$PROJECT" ]; then
  DIRS="$(cfg project-dirs "$PROJECT")"
  OPTIONAL_DIRS="$(cfg project-optional-dirs "$PROJECT")"
else
  DIRS="$(cfg account-dirs "$ACCOUNT")"
  OPTIONAL_DIRS="$(cfg account-optional-dirs "$ACCOUNT")"
fi
# Optional dirs (project key `optional_dirs`): the ones the task declares in
# `uses:` join DIRS (and so get a worktree below); every other one, and all
# of them for a session without a pre-selected task, is passed as its main
# checkout, read-only through the deny rules lib/permissions.py adds for it.
# Fail closed like the worktree step: an unreadable `uses:` launches nothing.
READONLY_DIRS="$OPTIONAL_DIRS"
if [ -n "$TASK_FILE" ] && [ -n "$PROJECT" ] && [ -n "$OPTIONAL_DIRS" ]; then
  WS_ERR="$STATE/workspace.$SLOT.txt"
  if ! OPT_SPLIT="$(python3 lib/workspace.py optional "$TASK_FILE" \
      "$BACKLOG_ROOT" $OPTIONAL_DIRS 2> "$WS_ERR")"; then
    echo "$(date '+%F %T') task=$(basename "$TASK_FILE") workspace failed, not launching: $(tr '\n' ' ' < "$WS_ERR")" >> "$STATE_ROOT/runs.log"
    rm -f "$WS_ERR"
    exit 1
  fi
  rm -f "$WS_ERR"
  READONLY_DIRS=""
  while IFS=$'\t' read -r kind d; do
    case "$kind" in
      use)      DIRS="${DIRS:+$DIRS }$d" ;;
      readonly) READONLY_DIRS="${READONLY_DIRS:+$READONLY_DIRS }$d" ;;
    esac
  done <<< "$OPT_SPLIT"
fi
# A pre-selected task never gets a repo's main checkout: lib/workspace.py
# swaps each repo dir for the task's own worktree (created or reused), unless
# the task declares `workdir: main`. Fail closed: a session that cannot get its
# worktree is not launched, rather than launched in a shared checkout.
# On failure it has already set the claimed task `blocked` with the reason.
if [ -n "$TASK_FILE" ] && [ -n "$PROJECT" ] && [ "$MODE" = "orchestrate" ]; then
  WS_ERR="$STATE/workspace.$SLOT.txt"
  if ! DIRS="$(python3 lib/workspace.py dirs "$TASK_FILE" "$BACKLOG_ROOT" \
      "$(cfg project-workdir "$PROJECT")" $DIRS 2> "$WS_ERR")"; then
    echo "$(date '+%F %T') task=$(basename "$TASK_FILE") workspace failed, not launching: $(tr '\n' ' ' < "$WS_ERR")" >> "$STATE_ROOT/runs.log"
    rm -f "$WS_ERR"
    exit 1
  fi
  rm -f "$WS_ERR"
  DIRS="$(printf '%s' "$DIRS" | tr '\n' ' ')"; DIRS="${DIRS% }"
fi
ADD_DIRS=(--add-dir "$BACKLOG_ROOT")
for d in $DIRS $READONLY_DIRS; do ADD_DIRS+=(--add-dir "$d"); done
ACCOUNT_PROJECTS="$(cfg account-projects "$ACCOUNT" | tr '\n' ' ')"

# The digest file this run journals into (or, for the digest session, the
# file it must curate). The day-boundary rule lives in lib/digest.py, reached
# through this one CLI subcommand so the shell never computes dates itself.
if [ "$MODE" = "digest" ]; then
  # The curator always curates TODAY's file. Pinning "now" to today's
  # midnight keeps it there even though the boundary rule (now >= digest_time
  # means tomorrow) would otherwise push a session starting right at
  # digest_time one day ahead.
  DIGEST_FILE="$(cfg digest-file "$(date '+%Y-%m-%dT00:00:00')")"
else
  DIGEST_FILE="$(cfg digest-file)"
fi

if [ -n "$TASK_FILE" ]; then
  DIRECTIVE="The gatekeeper already selected the task for this slice: $TASK_FILE. Work ONLY on that task and skip the selection in step 1."
  # A task stacked on an open PR got its worktree from that PR's branch
  # (lib/workspace.py): without the directive it would open its PR on main,
  # carrying the lower layer's commits. Fail closed.
  if [ "$MODE" = "orchestrate" ] && [ "$DELIVERY" = "pr" ]; then
    if ! STACK_DIRECTIVE="$(python3 lib/stacks.py directive "$BACKLOG_ROOT" "$TASK_FILE")"; then
      echo "$(date '+%F %T') task=$(basename "$TASK_FILE") stack directive failed, not launching" >> "$STATE_ROOT/runs.log"
      exit 1
    fi
    [ -n "$STACK_DIRECTIVE" ] && DIRECTIVE="$DIRECTIVE $STACK_DIRECTIVE"
  fi
else
  DIRECTIVE="No task was pre-selected: pick one yourself per step 1."
fi
case "$DELIVERY" in
  pr)     DELIVERY_DIRECTIVE="Delivery for this task is \`pr\`: you MUST push the branch and open a pull request, and record the PR URL in the task notes and in the digest. The task is not done until that PR exists." ;;
  branch) DELIVERY_DIRECTIVE="Delivery for this task is \`branch\`: commit on a dedicated branch and do NOT push. No PR." ;;
  local)  DELIVERY_DIRECTIVE="Delivery for this task is \`local\`: the strictly-local rails apply in full, nothing leaves the machine." ;;
  *)      DELIVERY_DIRECTIVE="No delivery was passed down: read the \`delivery:\` key of the task you pick and apply the contract in step 1." ;;
esac
# Permission routing by delivery. Sessions run in bypassPermissions, so the
# prompt's rails would otherwise rest on the session's good behaviour. A
# per-session deny is evaluated by the harness first (deny > ask > allow), so
# lib/permissions.py builds the rails as deny rules, one family per function
# with its reason: pushes and mutating gh calls for every delivery but `pr`
# (digest and auto modes carry no delivery and never publish either), force
# pushes, filter-branch and credential reads for every session, and writes
# under each read-only optional dir.
# Fail closed: a broken builder must not launch a session without its rails.
if ! RULES="$(python3 lib/permissions.py "$DELIVERY" $READONLY_DIRS)" || [ -z "$RULES" ]; then
  echo "$(date '+%F %T') permissions builder failed, not launching" >&2
  exit 1
fi
PERM_ARGS=(--disallowedTools)
while IFS= read -r rule; do PERM_ARGS+=("$rule"); done <<< "$RULES"
# The slice's absolute bounds go into the prompt: a headless session has no
# clock unless it runs `date`, and sessions told only "about N minutes" guessed
# their slice was over after a few minutes (2026-09-27).
if ! TIMES="$(python3 lib/prompt.py times "$SLICE_MIN")"; then
  echo "$(date '+%F %T') slice times failed, not launching" >&2
  exit 1
fi
SLICE_START="${TIMES%%$'\t'*}"; SLICE_DEADLINE="${TIMES#*$'\t'}"
if ! PROMPT="$(python3 lib/prompt.py render "prompts/$MODE.md" \
    "SLICE_MIN=$SLICE_MIN" "SLICE_START=$SLICE_START" "SLICE_DEADLINE=$SLICE_DEADLINE" \
    "TASK_DIRECTIVE=$DIRECTIVE" "BACKLOG_ROOT=$BACKLOG_ROOT" "ORCH_DIR=$ORCH_DIR" \
    "CONFIG_FILE=$CONFIG_FILE" "ACCOUNT=$ACCOUNT" \
    "ACCOUNT_PROJECTS=${ACCOUNT_PROJECTS% }" "PROJECT_DIRS=$BACKLOG_ROOT $DIRS" \
    "DELIVERY=$DELIVERY_DIRECTIVE" "DIGEST_FILE=$DIGEST_FILE")"; then
  echo "$(date '+%F %T') prompt rendering failed, not launching" >&2
  exit 1
fi
# Token budget warning (hooks/token_budget.py, registered in
# session-settings.json): the hook reads the task's raw `token_budget:` value
# from the environment and is a no-op without one (digest, auto-pick).
export ORCH_DIR
# Engine version stamped into the ledger: short SHA, "-dirty" when uncommitted.
ENGINE="$(git -C "$ORCH_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git -C "$ORCH_DIR" status --porcelain 2>/dev/null)" ]; then ENGINE="$ENGINE-dirty"; fi
PROMPT_SHA="$(printf '%s' "$PROMPT" | shasum -a 256 | cut -c1-12)"
ORCH_TOKEN_BUDGET=""
if [ -n "$TASK_FILE" ] && [ -f "$TASK_FILE" ]; then
  ORCH_TOKEN_BUDGET="$(sed -n '2,/^---$/s/^token_budget:[[:space:]]*//p' "$TASK_FILE" | head -1)"
fi
export ORCH_TOKEN_BUDGET
TIMEOUT_S=$(( (SLICE_MIN + 10) * 60 ))
START="$(date '+%F %T')"

# Platform seam: keep-awake.sh prevents idle system sleep for the duration of
# the run (night slots). Missing hook: run directly, staying awake is then the
# owner's problem, not a reason to lose the slice.
PLATFORM="${ORCH_PLATFORM:-}"
if [ -z "$PLATFORM" ] && [ "$(uname -s)" = "Darwin" ]; then PLATFORM=macos; fi
KEEP_AWAKE="$ORCH_DIR/platform/${PLATFORM:-none}/keep-awake.sh"
if [ ! -x "$KEEP_AWAKE" ]; then KEEP_AWAKE=""; fi

# The prompt goes through stdin: --add-dir is variadic and would swallow a
# positional prompt argument. The output feeds the per-account cost ledger
# (its final `result` line) and the rate-limit history (its
# `rate_limit_event` lines, which `--output-format json` would drop).
OUT_JSON="$STATE/result.$SLOT.json"
# stderr goes to a per-slot file first, then into the shared runs.out. It used
# to append straight to runs.out, which meant a session's own error text could
# not be told from the other three slots' - and the one line that matters
# ("You've hit your session limit - resets 4:20am") was therefore unusable.
ERR_FILE="$STATE/err.$SLOT.txt"
: > "$ERR_FILE"
# The session starts outside every repo (lib/workspace.py session_cwd): its
# cwd is writable whatever --add-dir says, and used to be the engine's own
# main checkout.
if ! SESSION_CWD="$(python3 lib/workspace.py session-cwd)"; then
  echo "$(date '+%F %T') session cwd failed, not launching" >&2
  exit 1
fi
printf '%s' "$PROMPT" | ( cd "$SESSION_CWD" && ${KEEP_AWAKE:+"$KEEP_AWAKE"} \
  python3 "$ORCH_DIR/lib/with_timeout.py" "$TIMEOUT_S" -- \
  "$CLAUDE_BIN" -p --output-format stream-json --verbose --model "$MODEL" --effort "$EFFORT" \
  --max-budget-usd "$MAX_USD" \
  --permission-mode bypassPermissions \
  --settings "$ORCH_DIR/session-settings.json" \
  ${PERM_ARGS[@]+"${PERM_ARGS[@]}"} \
  "${ADD_DIRS[@]}" ) \
  > "$OUT_JSON" 2> "$ERR_FILE"
CODE=$?
cat "$ERR_FILE" >> "$STATE_ROOT/runs.out"
# A failed session may have been refused by the account rather than have
# crashed: record which model is out of quota and until when, so the gatekeeper
# stops relaunching into a wall it has already hit. Only the model that failed
# is affected - the night that motivated this had Fable exhausted while Sonnet
# still ran fine.
if [ "$CODE" -ne 0 ]; then
  python3 lib/quota.py record "$STATE" "$MODEL" "$ERR_FILE" \
    >> "$STATE_ROOT/runs.out" 2>&1
fi
rm -f "$ERR_FILE"
# A real rate-limit reading from this session (lib/ratelimits.py headless),
# and a structured refusal signal when the event says `rejected`.
python3 lib/ratelimits.py headless "$ACCOUNT" "$MODEL" "$OUT_JSON" \
  >> "$STATE_ROOT/runs.out" 2>&1

python3 lib/ledger.py record "$STATE" "$OUT_JSON" "$MODE" "${TASK_FILE:-auto}" \
  "$MODEL" "$EFFORT" "$SLICE_MIN" "$CODE" "$ACCOUNT" "$EST_USD" "$ENGINE" "$PROMPT_SHA" \
  >> "$STATE_ROOT/runs.out" 2>&1
# Cost and duration for the digest journal's mechanical line, read from the
# result JSON before it is deleted.
read -r COST_USD DURATION_MIN < <(python3 lib/ledger.py fields "$OUT_JSON")
rm -f "$OUT_JSON"
# One normalized task= form (the basename), shared with orchestrate.md step 9
# and read by lib/stalls.py.
TASK_BASENAME="auto"
[ -n "$TASK_FILE" ] && TASK_BASENAME="$(basename "$TASK_FILE")"
# ORCH_LAUNCH=manual is set by manual.py: the owner launched this session by
# hand, which the digest must be able to tell from what the night decided.
LAUNCH_TAG=""; [ "${ORCH_LAUNCH:-}" = "manual" ] && LAUNCH_TAG=" launch=manual"
# A session that left more than half its slice unused while its task still has
# work left misjudged its time (lib/stalls.py early_exit). Tagged so the
# pattern shows in runs.log and the digest instead of hiding behind
# "stopped=slice end".
EARLY_TAG=""
if [ "$MODE" = "orchestrate" ] && [ -n "$TASK_FILE" ] && [ "$CODE" -eq 0 ] \
   && python3 lib/stalls.py early-exit "$SLICE_MIN" "$DURATION_MIN" "$TASK_FILE"; then
  EARLY_TAG=" early_exit"
fi
echo "$START mode=$MODE account=$ACCOUNT slot=$SLOT slice=${SLICE_MIN}min task=$TASK_BASENAME project=${PROJECT:-auto} model=$MODEL/$EFFORT${LAUNCH_TAG}${EARLY_TAG} exit=$CODE" >> "$STATE_ROOT/runs.log"

# Mechanical journal entry: one line per run, appended to this run's digest
# file (creating the header/section on first write). A single `>>` write per
# invocation - never split across two writes - because parallel slots append
# to the same file concurrently.
ENTRY_LINE="$(printf -- '- %s [%s/%s] %s (%s/%s, $%s, %smin of %s, exit %s%s%s)' \
  "$(date '+%H:%M')" "$ACCOUNT" "${PROJECT:-auto}" "$TASK_BASENAME" \
  "$MODEL" "$EFFORT" "$COST_USD" "$DURATION_MIN" "$SLICE_MIN" "$CODE" \
  "${LAUNCH_TAG:+, manual}" "${EARLY_TAG:+, early exit}")"
mkdir -p "$(dirname "$DIGEST_FILE")"
if [ -s "$DIGEST_FILE" ]; then
  printf '%s\n' "$ENTRY_LINE" >> "$DIGEST_FILE"
else
  printf '# Digest %s\n\n## Runs\n%s\n' "$(basename "$DIGEST_FILE" .md)" "$ENTRY_LINE" >> "$DIGEST_FILE"
fi
exit 0
