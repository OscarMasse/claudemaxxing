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
#   RUN <account> <slice> <task> <model> <effort> <project>
LAUNCHED=0
while IFS= read -r line; do
  case "$line" in
    RUN\ *)
      read -r _ ACCOUNT REST <<< "$line"
      # Word splitting of REST is intentional: "<slice> <task> <model> <effort> <project>".
      ./run.sh --account "$ACCOUNT" $REST &
      LAUNCHED=1
      ;;
  esac
done <<< "$DECISION"
# Wait for children: launchd kills the process group when this script exits.
[ "$LAUNCHED" = "1" ] && wait
exit 0
