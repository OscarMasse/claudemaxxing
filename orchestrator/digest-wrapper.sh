#!/bin/bash
# Morning digest entry point for the daily scheduled unit, with gatekeeper
# self-healing. The calendar-triggered digest job is the one scheduled spawn
# that fires reliably every day, so it doubles as a watchdog: if the
# gatekeeper loop is not running (launchd "pended spawn" limbo, crash),
# kickstart it through the platform hook.
set -u
cd "$(dirname "$0")"
export BACKLOG_ROOT="${BACKLOG_ROOT:-$(cd .. && pwd)}"
STATE_ROOT="$BACKLOG_ROOT/orchestrator/state"
mkdir -p "$STATE_ROOT"

if ! pgrep -f "gatekeeper-loop.sh" > /dev/null; then
  echo "$(date '+%F %T') digest-wrapper: gatekeeper loop not running, kickstarting" >> "$STATE_ROOT/gatekeeper.log"
  # Platform seam: the kickstart mechanism is init-system specific.
  PLATFORM="${ORCH_PLATFORM:-}"
  if [ -z "$PLATFORM" ] && [ "$(uname -s)" = "Darwin" ]; then PLATFORM=macos; fi
  KICKSTART="platform/${PLATFORM:-none}/kickstart-scheduler.sh"
  if [ -x "$KICKSTART" ]; then
    "$KICKSTART" 2>> "$STATE_ROOT/gatekeeper.log"
  else
    echo "$(date '+%F %T') digest-wrapper: no kickstart hook for platform ${PLATFORM:-unknown}" >> "$STATE_ROOT/gatekeeper.log"
  fi
fi

exec /bin/bash run.sh --digest
