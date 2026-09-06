#!/bin/bash
# Launch one background orchestrator session (or the morning digest).
# Usage: run.sh [--account NAME] <slice_min> [task_file] [model] [effort] [project]
#        run.sh --digest [--account NAME]
# The account may also come from the ORCH_ACCOUNT env var; without either, the
# first account in config.yaml is used. The account selects the Claude profile
# (CLAUDE_CONFIG_DIR), the binary, and the state/<account>/ namespace.
# Running this directly is the MANUAL trigger: it bypasses the gatekeeper's
# quota locks (but not the RUNNING lock) - you decide, it runs.
# Without a task_file the session picks the task itself (sonnet only), among
# the projects of this account.
set -uo pipefail
cd "$(dirname "$0")"
ORCH_DIR="$(pwd)"
# The backlog root (tasks/, digests/, NEEDS-HUMAN.md, orchestrator/state/)
# defaults to the repo root; export it so gate.py and lib/config.py resolve
# the same config and state paths (config resolution lives in lib/config.py).
export BACKLOG_ROOT="${BACKLOG_ROOT:-$(cd .. && pwd)}"
STATE_ROOT="$BACKLOG_ROOT/orchestrator/state"
# launchd hands down a bare PATH (/usr/bin:/bin:...), so node/npx are missing
# and anything the session shells out to that needs them (gate.py status ->
# ccusage via npx) dies with FileNotFoundError. Same export as gatekeeper.sh.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# GitHub credentials for background sessions: a fine-grained PAT scoped to the
# repos the agents may touch (Contents + Pull requests only). GH_TOKEN drives
# `gh` (pr create etc.); GIT_ASKPASS answers git's HTTPS prompts. Push targets
# are HTTPS URLs; protect main with pre-push hooks or branch protection.
AGENT_GH_TOKEN_FILE="$HOME/.config/backlog-agents/github-token"
if [ -f "$AGENT_GH_TOKEN_FILE" ]; then
  export GH_TOKEN="$(cat "$AGENT_GH_TOKEN_FILE")"
  export GIT_ASKPASS="$HOME/.config/backlog-agents/git-askpass.sh"
fi

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
if [ "$MODE" = "digest" ]; then SLICE_MIN=15; TASK_FILE=""; PROJECT=""; fi

# All config access goes through lib/config.py (accounts inherit flat keys).
# The live config file is resolved once, in the single place that owns the
# order: ORCH_CONFIG, then $BACKLOG_ROOT/config.yaml, then the repo default.
CONFIG_FILE="$(python3 lib/config.py resolve)"
cfg() { python3 lib/config.py "$CONFIG_FILE" "$@"; }
if [ -z "$ACCOUNT" ]; then ACCOUNT="$(cfg first-account)"; fi

# The account's Claude profile drives the invocation AND where ccusage /
# activity detection read, so each subscription is fully self-contained.
export CLAUDE_CONFIG_DIR="$(cfg account "$ACCOUNT" claude_config_dir)"
CLAUDE_BIN="$(cfg account "$ACCOUNT" claude_bin)"
MODEL="${MODEL_OVR:-$(cfg account "$ACCOUNT" claude_model)}"
EFFORT="${EFFORT_OVR:-$(cfg account "$ACCOUNT" claude_effort)}"
MAX_USD="$(cfg account "$ACCOUNT" max_session_usd 15)"

# Directories the session may write to (--add-dir): the picked task's project
# dirs, or every project dir of this account when no task was pre-selected.
# The backlog root itself is always included.
if [ -n "$PROJECT" ]; then
  DIRS="$(cfg project-dirs "$PROJECT")"
else
  DIRS="$(cfg account-dirs "$ACCOUNT")"
fi
ADD_DIRS=(--add-dir "$BACKLOG_ROOT")
for d in $DIRS; do ADD_DIRS+=(--add-dir "$d"); done
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

# Slot-based locks, namespaced per account: up to 8 concurrent sessions (the
# gatekeeper caps how many get launched per regime and per account; manual
# runs take a slot like any other). Accounts never contend for slots.
STATE="$STATE_ROOT/$ACCOUNT"
mkdir -p "$STATE"
SLOT=""
for i in 1 2 3 4 5 6 7 8; do
  if ( set -o noclobber; echo "$$ $(date +%s)" > "$STATE/RUNNING.$i" ) 2>/dev/null; then
    SLOT=$i; break
  fi
done
if [ -z "$SLOT" ]; then echo "no free slot account=$ACCOUNT" >> "$STATE_ROOT/runs.log"; exit 0; fi
LOCK="$STATE/RUNNING.$SLOT"
trap 'rm -f "$LOCK"' EXIT INT TERM

if [ -n "$TASK_FILE" ]; then
  DIRECTIVE="The gatekeeper already selected the task for this slice: $TASK_FILE. Work ONLY on that task and skip the selection in step 1."
else
  DIRECTIVE="No task was pre-selected: pick one yourself per step 1."
fi
PROMPT="$(sed -e "s/{{SLICE_MIN}}/$SLICE_MIN/g" -e "s|{{TASK_DIRECTIVE}}|$DIRECTIVE|g" \
              -e "s|{{BACKLOG_ROOT}}|$BACKLOG_ROOT|g" \
              -e "s|{{ORCH_DIR}}|$ORCH_DIR|g" \
              -e "s|{{CONFIG_FILE}}|$CONFIG_FILE|g" \
              -e "s|{{ACCOUNT}}|$ACCOUNT|g" \
              -e "s|{{ACCOUNT_PROJECTS}}|${ACCOUNT_PROJECTS% }|g" \
              -e "s|{{PROJECT_DIRS}}|$BACKLOG_ROOT $DIRS|g" \
              -e "s|{{DIGEST_FILE}}|$DIGEST_FILE|g" "prompts/$MODE.md")"
TIMEOUT_S=$(( (SLICE_MIN + 10) * 60 ))
START="$(date '+%F %T')"

# Platform seam: keep-awake.sh prevents idle system sleep for the duration of
# the run (night slots). Missing hook: run directly, staying awake is then the
# owner's problem, not a reason to lose the slice.
PLATFORM="${ORCH_PLATFORM:-}"
if [ -z "$PLATFORM" ] && [ "$(uname -s)" = "Darwin" ]; then PLATFORM=macos; fi
KEEP_AWAKE="platform/${PLATFORM:-none}/keep-awake.sh"
if [ ! -x "$KEEP_AWAKE" ]; then KEEP_AWAKE=""; fi

# The prompt goes through stdin: --add-dir is variadic and would swallow a
# positional prompt argument. JSON output feeds the per-account cost ledger.
OUT_JSON="$STATE/result.$SLOT.json"
printf '%s' "$PROMPT" | ${KEEP_AWAKE:+"$KEEP_AWAKE"} \
  python3 lib/with_timeout.py "$TIMEOUT_S" -- \
  "$CLAUDE_BIN" -p --output-format json --model "$MODEL" --effort "$EFFORT" \
  --max-budget-usd "$MAX_USD" \
  --permission-mode bypassPermissions \
  "${ADD_DIRS[@]}" \
  > "$OUT_JSON" 2>> "$STATE_ROOT/runs.out"
CODE=$?

python3 lib/ledger.py record "$STATE" "$OUT_JSON" "$MODE" "${TASK_FILE:-auto}" \
  "$MODEL" "$EFFORT" "$SLICE_MIN" "$CODE" "$ACCOUNT" >> "$STATE_ROOT/runs.out" 2>&1
# Cost and duration for the digest journal's mechanical line, read from the
# result JSON before it is deleted.
read -r COST_USD DURATION_MIN < <(python3 lib/ledger.py fields "$OUT_JSON")
rm -f "$OUT_JSON"
echo "$START mode=$MODE account=$ACCOUNT slot=$SLOT slice=${SLICE_MIN}min task=${TASK_FILE:-auto} project=${PROJECT:-auto} model=$MODEL/$EFFORT exit=$CODE" >> "$STATE_ROOT/runs.log"

# Mechanical journal entry: one line per run, appended to this run's digest
# file (creating the header/section on first write). A single `>>` write per
# invocation - never split across two writes - because parallel slots append
# to the same file concurrently.
TASK_BASENAME="auto"
[ -n "$TASK_FILE" ] && TASK_BASENAME="$(basename "$TASK_FILE")"
ENTRY_LINE="$(printf -- '- %s [%s/%s] %s (%s/%s, $%s, %smin, exit %s)' \
  "$(date '+%H:%M')" "$ACCOUNT" "${PROJECT:-auto}" "$TASK_BASENAME" \
  "$MODEL" "$EFFORT" "$COST_USD" "$DURATION_MIN" "$CODE")"
mkdir -p "$(dirname "$DIGEST_FILE")"
if [ -s "$DIGEST_FILE" ]; then
  printf '%s\n' "$ENTRY_LINE" >> "$DIGEST_FILE"
else
  printf '# Digest %s\n\n## Runs\n%s\n' "$(basename "$DIGEST_FILE" .md)" "$ENTRY_LINE" >> "$DIGEST_FILE"
fi
exit 0
