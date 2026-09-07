import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from lib import ledger


class TestLedger(unittest.TestCase):
    def test_stats_and_rates(self):
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
            r = ledger.rates(d)
            self.assertAlmostEqual(r[("tasks/a.md", "sonnet")], 750.0)  # 15000/20
            self.assertAlmostEqual(r[("tasks/b.md", "opus")], 2000.0)   # 40000/20

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


def entry(ts="2026-08-12T02:30:00+00:00", tokens=100, **kw):
    e = {"ts": ts, "task": "tasks/a.md", "model": "sonnet", "slice_min": 10,
         "input_tokens": tokens, "output_tokens": 0,
         "cache_read": 0, "cache_write": 0}
    e.update(kw)
    return e


class TestLearning(unittest.TestCase):
    """The feedback loop: what a night cost, and how good the estimates were."""

    def test_spent_since_counts_only_later_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jsonl(state, [entry(ts="2026-08-12T01:00:00+00:00", tokens=7),
                          entry(ts="2026-08-12T02:30:00+00:00", tokens=100),
                          entry(ts="2026-08-12T03:30:00+00:00", tokens=50)])
            since = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
            self.assertEqual(ledger.spent_since(state, since), 150)

    def test_spent_since_reads_naive_timestamps_as_utc(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jsonl(state, [entry(ts="2026-08-12T02:30:00", tokens=100)])
            since = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
            self.assertEqual(ledger.spent_since(state, since), 100)

    def test_spent_since_on_a_missing_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            since = datetime(2026, 8, 12, tzinfo=timezone.utc)
            self.assertEqual(ledger.spent_since(Path(d) / "nope", since), 0)

    def test_accuracy_scores_estimates_against_outcomes(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jsonl(state, [entry(tokens=200, est_tokens=100),
                          entry(tokens=100, est_tokens=100),
                          entry(tokens=999)])  # no estimate recorded: ignored
            a = ledger.accuracy(state)["tasks/a.md"]
            self.assertEqual((a["runs"], a["est_tokens"], a["actual_tokens"]),
                             (2, 200, 300))
            self.assertAlmostEqual(a["ratio"], 1.5)  # under-estimated by 50%

    def test_rates_only_average_the_recent_window(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            # Cheap survey slices first, then the task gets expensive: the
            # stale cheap runs must fall out of the window.
            jsonl(state, [entry(tokens=10)] * 5 + [entry(tokens=1000)] * 5)
            self.assertEqual(ledger.rates(state)[("tasks/a.md", "sonnet")], 100.0)

    def test_record_stores_the_launch_estimate(self):
        with tempfile.TemporaryDirectory() as d:
            result = Path(d) / "result.json"
            result.write_text(json.dumps({"usage": {}, "result": ""}))
            state = Path(d) / "state"
            ledger.record(state, result, "orchestrate", "tasks/a.md", "sonnet",
                          "low", 10, 0, "personal", 4200)
            self.assertEqual(json.loads((state / "costs.jsonl").read_text())
                             ["est_tokens"], 4200)


if __name__ == "__main__":
    unittest.main()
