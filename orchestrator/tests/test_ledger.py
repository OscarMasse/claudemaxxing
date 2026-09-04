import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
