#!/bin/bash
# Thin dispatcher: everything scheduler-specific lives in platform/<os>/.
# ORCH_PLATFORM overrides detection (tests, unusual setups).
set -euo pipefail
# Homebrew first: python3 must be 3.14, not the macOS system 3.9.
export PATH="${ORCH_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin}"
cd "$(dirname "$0")"
PLATFORM="${ORCH_PLATFORM:-}"
if [ -z "$PLATFORM" ]; then
  case "$(uname -s)" in
    Darwin) PLATFORM=macos ;;
    *) PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
  esac
fi
if [ ! -x "platform/$PLATFORM/install.sh" ]; then
  echo "no platform adapter for $PLATFORM, see platform/README.md" >&2
  exit 1
fi
# Record the backlog root so interactive shells (gate.py status, manual.sh)
# resolve the same root as the scheduled jobs without BACKLOG_ROOT exported.
# BACKLOG_ROOT wins; a reinstall without it keeps the recorded root.
BACKLOG_ROOT="${BACKLOG_ROOT:-$(python3 lib/config.py backlog-root)}"
BACKLOG_ROOT="$(cd "$BACKLOG_ROOT" && pwd)"
export BACKLOG_ROOT
echo "backlog=$BACKLOG_ROOT recorded in $(python3 lib/config.py record-root "$BACKLOG_ROOT")"
exec "platform/$PLATFORM/install.sh"
