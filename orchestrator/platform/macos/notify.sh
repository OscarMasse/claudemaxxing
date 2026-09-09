#!/bin/bash
# macOS adapter: show a user notification. Args: <title> <message> [path].
#
# When `path` is given the notification is clickable and opens that file. That
# needs terminal-notifier: `osascript -e 'display notification'` cannot carry
# an action, and the notification it posts is attributed to Script Editor, so
# clicking it opens Script Editor instead of anything useful.
# terminal-notifier also needs its own notification permission (System
# Settings > Notifications), which it cannot always prompt for. So it is tried
# rather than trusted: whenever it is missing OR refuses, the osascript path
# still posts the notification, just without the click action. Losing the
# click is a downgrade; losing the alert would be a bug.
#
# The command used to open the file comes from ORCH_NOTIFY_OPEN (set by gate.py
# from `notify_open_cmd` in config.yaml), defaulting to `open`, which hands the
# file to whatever application macOS has registered for it.
set -u
TITLE="${1:?usage: notify.sh <title> <message> [path]}"
MSG="${2:?usage: notify.sh <title> <message> [path]}"
PATH_ARG="${3:-}"
OPEN_CMD="${ORCH_NOTIFY_OPEN:-open}"
# Resolved to an absolute path: the click is executed later, by an app whose
# environment is launchd's, where /opt/homebrew/bin is not on the PATH. A bare
# `zed` would simply do nothing.
OPEN_ABS="$(command -v "$OPEN_CMD" 2>/dev/null || printf '%s' "$OPEN_CMD")"

if [ -n "$PATH_ARG" ] && command -v terminal-notifier >/dev/null 2>&1; then
  # -execute runs through a shell, so the path is single-quoted against spaces.
  if terminal-notifier -title "$TITLE" -message "$MSG" \
       -execute "$OPEN_ABS '${PATH_ARG//\'/\'\\\'\'}'"; then
    exit 0
  fi
fi

# osascript string literals: backslashes and double quotes must be escaped.
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
exec osascript -e "display notification \"$(esc "$MSG")\" with title \"$(esc "$TITLE")\""
