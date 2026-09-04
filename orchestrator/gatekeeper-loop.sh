#!/bin/bash
# Long-lived wrapper around gatekeeper.sh, run under launchd KeepAlive.
# Rationale: StartInterval agents can get stuck in launchd's "pended
# nondemand spawn" state after DarkWake cycles (observed 2026-08-12->14:
# two nights lost). A KeepAlive loop has no scheduled-spawn state to lose:
# the process ticks itself, and launchd only has to restart it if it dies.
set -u
cd "$(dirname "$0")"
export BACKLOG_ROOT="${BACKLOG_ROOT:-$(cd .. && pwd)}"
STATE_ROOT="$BACKLOG_ROOT/orchestrator/state"
mkdir -p "$STATE_ROOT"
INTERVAL=1800
echo "$(date '+%F %T') gatekeeper-loop started pid $$" >> "$STATE_ROOT/gatekeeper.log"

while true; do
  /bin/bash gatekeeper.sh
  sleep "$INTERVAL"
done
