#!/bin/bash
# Long-lived wrapper around gatekeeper.sh, run under launchd KeepAlive.
# Rationale: StartInterval agents can get stuck in launchd's "pended
# nondemand spawn" state after DarkWake cycles (observed 2026-08-12->14:
# two nights lost). A KeepAlive loop has no scheduled-spawn state to lose:
# the process ticks itself, and launchd only has to restart it if it dies.
set -u
# Homebrew first: python3 must be 3.14, not the macOS system 3.9.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
cd "$(dirname "$0")" || exit 1
# Resolved by lib/config.py (env, then the file install.sh records); exits
# non-zero with a message naming both when neither says where the backlog is.
BACKLOG_ROOT="$(python3 lib/config.py backlog-root)" || exit 2
export BACKLOG_ROOT
STATE_ROOT="$BACKLOG_ROOT/orchestrator/state"
mkdir -p "$STATE_ROOT"
# Tick interval. A tick is pure local Python (~1s, zero tokens), so the cost of
# ticking often is negligible and the benefit is twofold: a slot that comes free
# is refilled within one interval instead of one half-hour, and the activity
# lock (activity_idle_night_min) costs what it says rather than being rounded up
# to the next tick. Not lower than this on purpose: run.sh takes its RUNNING
# lock a few milliseconds in, but the gatekeeper's own view of free slots comes
# from locks written by the PREVIOUS tick's launches, so ticks that overlap a
# launch could double-book a slot.
INTERVAL=300
echo "$(date '+%F %T') gatekeeper-loop started pid $$" >> "$STATE_ROOT/gatekeeper.log"

while true; do
  /bin/bash gatekeeper.sh
  sleep "$INTERVAL"
done
