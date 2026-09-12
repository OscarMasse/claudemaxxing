import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from lib import ledger

LEDGER_PY = Path(__file__).resolve().parents[1] / "lib" / "ledger.py"


class TestLedger(unittest.TestCase):
    def test_stats_and_session_costs(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "costs.jsonl"
            entries = [
                {"task": "tasks/a.md", "model": "sonnet", "slice_min": 10,
                 "cost_usd": 0.5, "input_tokens": 100, "output_tokens": 400,
                 "cache_read": 4000, "cache_write": 500},
                {"task": "tasks/a.md", "model": "sonnet", "slice_min": 10,
                 "cost_usd": 0.5, "input_tokens": 100, "output_tokens": 400,
                 "cache_read": 9000, "cache_write": 500},
                {"task": "tasks/b.md", "model": "opus", "slice_min": 20,
                 "cost_usd": 2.0, "input_tokens": 0, "output_tokens": 1000,
                 "cache_read": 39000, "cache_write": 0},
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            s = ledger.stats(d)
            self.assertEqual(s["tasks/a.md"]["runs"], 2)
            self.assertAlmostEqual(s["tasks/a.md"]["cost_usd"], 1.0)
            self.assertEqual(s["tasks/a.md"]["total_tokens"], 15000)
            # Per session and in USD, so the differing slice_min values and
            # the token volumes are irrelevant: a task's cost is a property of
            # the work, not of the time it was allotted.
            r = ledger.session_costs(d)
            self.assertAlmostEqual(r[("tasks/a.md", "sonnet")], 0.5)
            self.assertAlmostEqual(r[("tasks/b.md", "opus")], 2.0)  # one run

    def test_record_writes_account_into_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = Path(d) / "result.json"
            result.write_text(json.dumps({
                "total_cost_usd": 0.42, "num_turns": 3, "duration_ms": 1000,
                "usage": {"input_tokens": 10, "output_tokens": 20,
                          "cache_read_input_tokens": 30,
                          "cache_creation_input_tokens": 40},
                "result": "done"}))
            state = Path(d) / "state" / "work"
            text = ledger.record(state, result, "orchestrate", "tasks/a.md",
                                 "sonnet", "low", 15, 0, "work")
            self.assertEqual(text, "done")
            entry = json.loads((state / "costs.jsonl").read_text())
            self.assertEqual(entry["account"], "work")
            self.assertEqual(entry["task"], "tasks/a.md")
            self.assertAlmostEqual(entry["cost_usd"], 0.42)

    def test_ledgers_are_isolated_per_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = Path(d) / "result.json"
            result.write_text(json.dumps({"usage": {}, "result": ""}))
            ledger.record(Path(d) / "a", result, "orchestrate", "tasks/x.md",
                          "sonnet", "low", 10, 0, "a")
            self.assertEqual(ledger.stats(Path(d) / "a")["tasks/x.md"]["runs"], 1)
            self.assertEqual(ledger.stats(Path(d) / "b"), {})


def jsonl(state, entries):
    state.mkdir(parents=True, exist_ok=True)
    (state / "costs.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries))


def entry(ts="2026-08-12T02:30:00+00:00", usd=1.0, **kw):
    e = {"ts": ts, "task": "tasks/a.md", "model": "sonnet", "slice_min": 10,
         "cost_usd": usd, "input_tokens": 100, "output_tokens": 0,
         "cache_read": 0, "cache_write": 0}
    e.update(kw)
    return e


class TestLearning(unittest.TestCase):
    """The feedback loop: what a night cost, and how good the estimates were."""

    def test_spent_since_counts_only_later_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jsonl(state, [entry(ts="2026-08-12T01:00:00+00:00", usd=0.7),
                          entry(ts="2026-08-12T02:30:00+00:00", usd=1.0),
                          entry(ts="2026-08-12T03:30:00+00:00", usd=0.5)])
            since = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
            self.assertAlmostEqual(ledger.spent_since(state, since), 1.5)

    def test_spent_since_reads_naive_timestamps_as_utc(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jsonl(state, [entry(ts="2026-08-12T02:30:00", usd=1.0)])
            since = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
            self.assertAlmostEqual(ledger.spent_since(state, since), 1.0)

    def test_spent_since_on_a_missing_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            since = datetime(2026, 8, 12, tzinfo=timezone.utc)
            self.assertEqual(ledger.spent_since(Path(d) / "nope", since), 0)

    def test_accuracy_scores_estimates_against_outcomes(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jsonl(state, [entry(usd=2.0, est_usd=1.0),
                          entry(usd=1.0, est_usd=1.0),
                          entry(usd=9.99)])  # no estimate recorded: ignored
            a = ledger.accuracy(state)["tasks/a.md"]
            self.assertEqual(a["runs"], 2)
            self.assertAlmostEqual(a["est_usd"], 2.0)
            self.assertAlmostEqual(a["actual_usd"], 3.0)
            self.assertAlmostEqual(a["ratio"], 1.5)  # under-estimated by 50%

    def test_session_costs_only_look_at_the_recent_window(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            # An expensive run first, then the task gets cheap for good: the
            # stale expensive run must fall out of the window, or the max
            # would quote it forever.
            jsonl(state, [entry(usd=50.0)] + [entry(usd=1.0)] * 5)
            self.assertEqual(
                ledger.session_costs(state)[("tasks/a.md", "sonnet")], 1.0)

    def test_session_costs_take_the_max_of_the_window_not_the_mean(self):
        # 2026-09-12: a 1M-token survey slice followed by a 25-minute
        # implementation slice; the mean kept quoting the survey and the
        # night launched sessions it could not pay for.
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jsonl(state, [entry(usd=0.2), entry(usd=3.0), entry(usd=0.4)])
            self.assertAlmostEqual(
                ledger.session_costs(state)[("tasks/a.md", "sonnet")], 3.0)

    def test_entries_without_a_cost_count_as_runs_but_cost_nothing(self):
        # A failed run, or one recorded before cost_usd existed.
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jsonl(state, [entry(usd=None), entry(usd=2.0)])
            self.assertEqual(ledger.stats(state)["tasks/a.md"]["runs"], 2)
            self.assertAlmostEqual(
                ledger.session_costs(state)[("tasks/a.md", "sonnet")], 2.0)
            since = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
            self.assertAlmostEqual(ledger.spent_since(state, since), 2.0)

    def test_session_cost_ignores_the_slice_length(self):
        # The regression this guards: the figure used to be per minute and the
        # caller multiplied by the slice again. Two identical sessions
        # allotted different slices must cost the same.
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jsonl(state, [entry(usd=5.0, slice_min=10),
                          entry(usd=5.0, slice_min=50)])
            self.assertEqual(
                ledger.session_costs(state)[("tasks/a.md", "sonnet")], 5.0)

    def test_record_stores_the_launch_estimate(self):
        with tempfile.TemporaryDirectory() as d:
            result = Path(d) / "result.json"
            result.write_text(json.dumps({"usage": {}, "result": ""}))
            state = Path(d) / "state"
            ledger.record(state, result, "orchestrate", "tasks/a.md", "sonnet",
                          "low", 10, 0, "personal", 4.2)
            self.assertAlmostEqual(json.loads((state / "costs.jsonl").read_text())
                                   ["est_usd"], 4.2)

    def test_cli_record_takes_the_estimate_positionally_as_run_sh_passes_it(self):
        # run.sh hands the gatekeeper's RUN-line estimate through unchanged;
        # the ledger must read the two-decimal string as dollars.
        with tempfile.TemporaryDirectory() as d:
            result = Path(d) / "result.json"
            result.write_text(json.dumps({"total_cost_usd": 3.1, "usage": {},
                                          "result": "ok"}))
            state = Path(d) / "state"
            r = subprocess.run([sys.executable, str(LEDGER_PY), "record",
                                str(state), str(result), "orchestrate",
                                "tasks/a.md", "sonnet", "low", "50", "0",
                                "personal", "2.50"],
                               capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "ok", r.stderr)
            e = json.loads((state / "costs.jsonl").read_text())
            self.assertAlmostEqual(e["est_usd"], 2.5)
            self.assertAlmostEqual(e["cost_usd"], 3.1)


if __name__ == "__main__":
    unittest.main()
