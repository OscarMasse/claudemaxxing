#!/bin/bash
# launchd entrypoint: one tick = one decision per account, maybe several runs.
set -uo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
# Backlog root: env (set by the launchd plist when installed against an
# external backlog) or the repo root. Exported so gate.py and run.sh resolve
# config and state against the same root.
export BACKLOG_ROOT="${BACKLOG_ROOT:-$(cd .. && pwd)}"
STATE_ROOT="$BACKLOG_ROOT/orchestrator/state"
mkdir -p "$STATE_ROOT"

DECISION="$(python3 gate.py tick 2>> "$STATE_ROOT/gatekeeper.log")" || exit 0
# One RUN line per session to launch (parallel slots, possibly on several
# accounts in the same tick). Format:
#   RUN <account> <slice> <task> <model> <effort> <project> <est_tokens>
#       <delivery>
LAUNCHED=0
while IFS= read -r line; do
  case "$line" in
    RUN\ *)
      read -r _ ACCOUNT REST <<< "$line"
      # Word splitting of REST is intentional:
      # "<slice> <task> <model> <effort> <project> <est_tokens> <delivery>".
      ./run.sh --account "$ACCOUNT" $REST &
      LAUNCHED=1
      ;;
  esac
done <<< "$DECISION"
# Deliberately NOT waiting for the children. This used to `wait`, which made a
# tick last as long as its longest session and turned the whole night into a
# batch: with four slots launched at 02:00 and one session running 20 minutes,
# the loop slept until 02:50 and the other three slots sat idle for 78 of every
# 80 minutes. Returning immediately makes it a pipeline instead - the next tick
# refills whatever slot has come free.
# The children survive because launchd's job is gatekeeper-loop.sh, which never
# exits: the process group outlives this script. Do not register gatekeeper.sh
# directly with launchd, or its exit will kill the sessions it just launched.
exit 0
