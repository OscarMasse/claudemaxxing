#!/bin/bash
# Manual batch launch (see manual.py for the modes and the rules it keeps).
#   manual.sh --for 5h | --count N | --tasks a b ... [options]
#   manual.sh ... --dry-run    print the plan, launch nothing
#   manual.sh --stop           stop every manual runner (sessions keep running)
# The runner detaches: it survives closing the terminal, and it runs under the
# platform keep-awake hook so an idle machine does not sleep between sessions -
# on battery too (caffeinate -i holds without AC; a closed lid still sleeps).
set -uo pipefail
cd "$(dirname "$0")" || exit 1
export PATH="${ORCH_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin}"
# Resolved by lib/config.py (env, then the file install.sh records); exits
# non-zero with a message naming both when neither says where the backlog is.
BACKLOG_ROOT="$(python3 lib/config.py backlog-root)" || exit 2
export BACKLOG_ROOT
STATE_ROOT="$BACKLOG_ROOT/orchestrator/state"
mkdir -p "$STATE_ROOT"

if [ "${1:-}" = "--stop" ]; then
  # Each runner leaves a state/<account>/MANUAL.<pid> marker while it lives.
  found=0
  for marker in "$STATE_ROOT"/*/MANUAL.*; do
    [ -e "$marker" ] || continue
    pid="${marker##*.}"
    if kill "$pid" 2>/dev/null; then
      echo "stopped manual runner pid $pid: $(cat "$marker")"; found=1
    else
      rm -f "$marker"  # its runner is already gone
    fi
  done
  [ "$found" = 1 ] || echo "no manual runner running"
  exit 0
fi

for a in "$@"; do
  case "$a" in
    --dry-run|-h|--help) exec python3 manual.py "$@" ;;
  esac
done
# Validate in the foreground: once detached, an error would only reach a log.
python3 manual.py "$@" --check || exit $?

PLATFORM="${ORCH_PLATFORM:-}"
if [ -z "$PLATFORM" ] && [ "$(uname -s)" = "Darwin" ]; then PLATFORM=macos; fi
KEEP_AWAKE="platform/${PLATFORM:-none}/keep-awake.sh"
if [ ! -x "$KEEP_AWAKE" ]; then KEEP_AWAKE=""; fi

nohup ${KEEP_AWAKE:+"$KEEP_AWAKE"} python3 manual.py "$@" \
  >> "$STATE_ROOT/manual.out" 2>&1 < /dev/null &
echo "manual runner started. Runner lines: grep -E 'account=[a-z0-9_-]+ manual ' $STATE_ROOT/gatekeeper.log | tail"
echo "Session progress: the task file's ## Notes, and $STATE_ROOT/manual.out - stop: $0 --stop"
