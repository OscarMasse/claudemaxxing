#!/bin/bash
# End-to-end rehearsal of one night, in a throwaway backlog, with a stubbed
# `claude` binary. Zero tokens, zero effect on the real backlog: the whole
# chain runs (gate.py tick -> gatekeeper.sh -> run.sh -> ledger), so it proves
# the scheduling decisions AND the shell plumbing, not just the Python.
#
# Usage: orchestrator/e2e-sandbox.sh [sandbox_dir]
#
# What it demonstrates, in order: a nightly duty jumping ahead of a
# higher-priority queue task, the queue, a filler taking the leftover budget,
# the duty period being consumed so a second tick the same night does not
# repeat it, and the estimate-vs-actual line the planner learns from.
set -uo pipefail
cd "$(dirname "$0")"
SANDBOX="${1:-$(mktemp -d "${TMPDIR:-/tmp}/orch-e2e.XXXXXX")}"
NIGHT="2026-09-08T02:30:00+02:00"  # Tuesday, 4 nights before the Thursday reset

rm -rf "$SANDBOX"
mkdir -p "$SANDBOX"/{tasks,orchestrator/state,proj,cfgdir/projects}
# Normalize the path (mktemp inherits any trailing slash from TMPDIR), so the
# `sed` filters below actually match the paths the engine prints.
SANDBOX="$(cd "$SANDBOX" && pwd -P)"

cat > "$SANDBOX/claude-stub.sh" <<'STUB'
#!/bin/bash
cat > /dev/null  # swallow the prompt on stdin
echo '{"total_cost_usd":0.11,"num_turns":3,"duration_ms":120000,"usage":{"input_tokens":4,"output_tokens":2,"cache_read_input_tokens":1,"cache_creation_input_tokens":0},"result":"stub session"}'
STUB
chmod +x "$SANDBOX/claude-stub.sh"

# Quota fixture: a usage snapshot with no consumption and no open 5h window,
# so the sandbox night starts from a full budget.
cat > "$SANDBOX/usage.json" <<'FIXTURE'
{"week_usd":0,"week_by_family":{},"block":null,"unknown_models":[]}
FIXTURE

# Small dollar figures keep the arithmetic readable in the output.
cat > "$SANDBOX/config.yaml" <<CFG
dry_run: false
night_start: 02:00
night_end: 06:00
morning_guard: 08:30
prereset_burn_hours: 8
activity_idle_night_min: 40
night_slice_min: 50
max_parallel_sessions: 4
night_budget_ratio: 2.0
est_session_usd: 2.5
claude_bin: $SANDBOX/claude-stub.sh
claude_model: sonnet
claude_effort: low
max_session_usd: 1
accounts:
  - name: max
    claude_config_dir: $SANDBOX/cfgdir
    reset_weekday: 4
    reset_time: 05:59
    reset_tz: Europe/Warsaw
projects:
  - name: demo
    account: max
    dirs: $SANDBOX/proj
    priority: 10
CFG

task() {  # task <file> <priority> [extra frontmatter line]
  printf -- '---\ntitle: %s\nproject: demo\nstatus: ready\npriority: %s\ndelivery: branch\ncreated: 2026-08-01\n%s---\n\nBody.\n' \
    "$1" "$2" "${3:+$3
}" > "$SANDBOX/tasks/$1"
}
task queue.md high
task sync.md low "duty: nightly"
task tidy.md low "filler: true"

export BACKLOG_ROOT="$SANDBOX" ORCH_ROOT="$SANDBOX" ORCH_CONFIG="$SANDBOX/config.yaml"
export ORCH_IDLE_MIN=999 ORCH_NO_NOTIFY=1 ORCH_USAGE_JSON="$SANDBOX/usage.json"
# Caps are derived from the rate_limits history; the sandbox has no status
# line feeding it, so it starts from a seed row (the caps used as-is).
python3 lib/ratelimits.py seed max 1000 200 50 2026-08-01T00:00:00+00:00 > /dev/null

echo "sandbox: $SANDBOX"
echo
echo "== plan (projected allocation per remaining night) =="
ORCH_NOW="$NIGHT" python3 gate.py plan
echo
echo "== one full night tick (duty, queue, filler) =="
ORCH_NOW="$NIGHT" ./gatekeeper.sh
# gatekeeper.sh deliberately does not wait for the sessions it launches (it is
# a pipeline, not a batch - see the comment at the end of it), so a rehearsal
# has to wait for them itself before reading what they logged.
for _ in $(seq 1 100); do
  [ -f "$SANDBOX/orchestrator/state/runs.log" ] && [ -z "$(ls "$SANDBOX"/orchestrator/state/max/RUNNING.* 2>/dev/null)" ] && break
  sleep 0.2
done
grep -o 'tasks/[a-z]*\.md' "$SANDBOX/orchestrator/state/runs.log" | sort | sed 's/^/launched /'
echo
echo "== duty period consumed =="
cat "$SANDBOX/orchestrator/state/max/duties.json"
echo
echo "== second tick, same night: the duty must not repeat =="
ORCH_NOW="2026-09-08T03:30:00+02:00" python3 gate.py tick | sed "s|$SANDBOX/tasks/||"
echo
echo "== what the planner learned =="
ORCH_NOW="$NIGHT" python3 gate.py status | grep -E '^(night_budget|duty|filler|estimate)' \
  | sed "s|$SANDBOX/tasks/||"
