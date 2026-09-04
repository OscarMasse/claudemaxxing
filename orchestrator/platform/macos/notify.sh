#!/bin/bash
# macOS adapter: show a user notification. Args: <title> <message>.
set -u
TITLE="${1:?usage: notify.sh <title> <message>}"
MSG="${2:?usage: notify.sh <title> <message>}"
# osascript string literals: backslashes and double quotes must be escaped.
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
exec osascript -e "display notification \"$(esc "$MSG")\" with title \"$(esc "$TITLE")\""
