#!/usr/bin/env bash
# The one gate: CI runs exactly this script, so local and CI cannot drift.
# Needs python3 plus ruff and shellcheck (from PATH, else fetched with uvx).
set -euo pipefail
cd "$(dirname "$0")"

tool() {
  local name=$1 pkg=$2
  shift 2
  if command -v "$name" >/dev/null 2>&1; then
    "$name" "$@"
  else
    uvx --quiet --from "$pkg" "$name" "$@"
  fi
}
export -f tool

echo "== config parses =="
python3 -c '
import json, sys
sys.path.insert(0, "orchestrator")
from lib import config
cfg = config.load("orchestrator/config.toml")
assert cfg, "orchestrator/config.toml parsed to nothing"
json.load(open("orchestrator/session-settings.json"))
'

echo "== ruff =="
tool ruff ruff check .

echo "== shellcheck =="
find orchestrator -name '*.sh' -print0 | xargs -0 bash -c 'tool shellcheck shellcheck-py -S warning "$@"' _

echo "== unit tests =="
python3 -m unittest discover -s orchestrator/tests -t orchestrator

echo "all checks passed"
