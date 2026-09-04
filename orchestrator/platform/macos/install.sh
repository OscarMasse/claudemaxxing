#!/bin/bash
# macOS adapter: install (or reinstall) the orchestrator LaunchAgents. Idempotent.
# The launchd/ plists are templates, substituted at install time:
#   __ORCH_DIR__      -> this checkout's orchestrator/ (where the code lives)
#   __BACKLOG_ROOT__  -> the backlog root (tasks/, digests/, state/, config)
#   __DIGEST_HOUR__   -> Hour of the digest StartCalendarInterval
#   __DIGEST_MINUTE__ -> Minute of the digest StartCalendarInterval
# BACKLOG_ROOT may be set in the environment to run the engine from this
# checkout against a separate backlog directory; the default is the repo root
# (the historical layout where the checkout doubles as the backlog root).
set -euo pipefail
cd "$(dirname "$0")"
ORCH_DIR="$(cd ../.. && pwd)"
BACKLOG_ROOT="${BACKLOG_ROOT:-$(cd "$ORCH_DIR/.." && pwd)}"
BACKLOG_ROOT="$(cd "$BACKLOG_ROOT" && pwd)"
UID_N=$(id -u)
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
      -e "s|__DIGEST_MINUTE__|$DIGEST_MINUTE|g" "launchd/$plist" > ~/Library/LaunchAgents/"$plist"
  launchctl bootout "gui/$UID_N" ~/Library/LaunchAgents/"$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" ~/Library/LaunchAgents/"$plist"
done
launchctl list | grep local.backlog
echo
echo "MANUAL STEP (needs sudo, run yourself): schedule a nightly wake before"
echo "the night regime starts, e.g.:"
echo "  sudo pmset repeat wakeorpoweron MTWRFSU 01:55:00"
echo "Verify with: pmset -g sched"
