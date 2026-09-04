#!/bin/bash
# macOS adapter: run the given command while preventing idle system sleep.
# caffeinate -i holds the assertion exactly as long as the child lives.
set -u
exec caffeinate -i "$@"
