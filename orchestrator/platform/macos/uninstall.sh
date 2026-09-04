#!/bin/bash
# macOS adapter: remove the orchestrator LaunchAgents. Idempotent.
# Running sessions are not killed; only future scheduling stops.
set -euo pipefail
UID_N=$(id -u)
for name in gatekeeper digest; do
  plist="$HOME/Library/LaunchAgents/local.backlog.$name.plist"
  launchctl bootout "gui/$UID_N" "$plist" 2>/dev/null || true
  rm -f "$plist"
done
echo "LaunchAgents removed."
echo "If you scheduled a nightly wake, cancel it yourself:"
echo "  sudo pmset repeat cancel"
