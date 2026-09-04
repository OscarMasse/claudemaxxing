#!/bin/bash
# Thin dispatcher: everything scheduler-specific lives in platform/<os>/.
# ORCH_PLATFORM overrides detection (tests, unusual setups).
set -euo pipefail
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
exec "platform/$PLATFORM/install.sh"
