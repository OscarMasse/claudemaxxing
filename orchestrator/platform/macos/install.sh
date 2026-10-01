#!/bin/bash
# macOS adapter: install (or reinstall) the orchestrator LaunchAgents. Idempotent.
# The launchd/ plists are templates, substituted at install time:
#   __ORCH_DIR__      -> this checkout's orchestrator/ (where the code lives)
#   __BACKLOG_ROOT__  -> the backlog root (tasks/, digests/, state/, config)
#   __DIGEST_HOUR__   -> Hour of the digest StartCalendarInterval
#   __DIGEST_MINUTE__ -> Minute of the digest StartCalendarInterval
#   __PATH__          -> the jobs' PATH, Homebrew first so python3 is 3.14
# BACKLOG_ROOT comes from the dispatcher (../../install.sh), which resolves
# and records it.
set -euo pipefail
cd "$(dirname "$0")"
ORCH_DIR="$(cd ../.. && pwd)"
BACKLOG_ROOT="$(cd "${BACKLOG_ROOT:?run orchestrator/install.sh, not this adapter}" && pwd)"
UID_N=$(id -u)
export PATH="${ORCH_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin}"
JOB_PATH="$PATH"
mkdir -p "$BACKLOG_ROOT/orchestrator/state"

# digest_time drives the digest job's schedule; read it through config.py so
# the parsing logic stays in one place (same pattern run.sh uses for cfg).
CONFIG_FILE="$(BACKLOG_ROOT="$BACKLOG_ROOT" python3 "$ORCH_DIR/lib/config.py" resolve)"
DIGEST_TIME="$(python3 "$ORCH_DIR/lib/config.py" "$CONFIG_FILE" get digest_time 07:37)"
# Base-10 forced: a leading zero (07, 09) would otherwise be read as octal.
DIGEST_HOUR="$((10#${DIGEST_TIME%%:*}))"
DIGEST_MINUTE="$((10#${DIGEST_TIME##*:}))"

for name in gatekeeper digest; do
  plist="local.backlog.$name.plist"
  sed -e "s|__ORCH_DIR__|$ORCH_DIR|g" \
      -e "s|__BACKLOG_ROOT__|$BACKLOG_ROOT|g" \
      -e "s|__DIGEST_HOUR__|$DIGEST_HOUR|g" \
      -e "s|__DIGEST_MINUTE__|$DIGEST_MINUTE|g" \
      -e "s|__PATH__|$JOB_PATH|g" "launchd/$plist" > ~/Library/LaunchAgents/"$plist"
  launchctl bootout "gui/$UID_N" ~/Library/LaunchAgents/"$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" ~/Library/LaunchAgents/"$plist"
done
launchctl list | grep local.backlog
echo
echo "MANUAL STEP (needs sudo, run yourself): schedule a nightly wake before"
echo "the night regime starts, e.g.:"
echo "  sudo pmset repeat wakeorpoweron MTWRFSU 01:55:00"
echo "Verify with: pmset -g sched"
