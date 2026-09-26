import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ORCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH))
from lib import stalls, tasks  # noqa: E402

TASK = ("---\ntitle: T\nproject: p\nstatus: ready\ndelivery: local\n---\n\n"
        "## Notes\n\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()
        self.state = self.root / "orchestrator" / "state"
        self.state.mkdir(parents=True)
        self.t0 = datetime(2026, 9, 10, 2, 0)
        self.now = self.t0 + timedelta(hours=6)

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, name="a.md", text=TASK):
        path = self.root / "tasks" / name
        path.write_text(text)
        return path

    def session(self, path, ts, note=None, spelling=None, code=0):
        """One launch as the engine performs it: hash, claim, session, log."""
        stalls.record_launch(self.state, path, ts)
        tasks.claim(path, ts.strftime("%F"), "opus", 50)
        if note:
            tasks.set_status(path, "ready", f"- {note}\n")
        else:
            tasks.set_status(path, "ready", "")
            # set_status always appends; undo so the file is truly unchanged
            path.write_text(path.read_text().rstrip("\n") + "\n")
        raw = spelling or str(path)
        with open(self.state / "runs.log", "a") as f:
            f.write(f"{ts:%F %T} mode=orchestrate account=max slot=1 "
                    f"slice=50min task={raw} project=p model=opus/low "
                    f"exit={code}\n")


class StallTest(Base):
    def test_unchanged_file_is_stalled(self):
        path = self.task()
        for i in range(5):
            self.session(path, self.t0 + timedelta(days=i))
        found = stalls.detect(self.root, self.state, self.now)
        self.assertEqual([(f[0], f[1], f[2]) for f in found],
                         [("a.md", "stalled", 5)])

    def test_growing_notes_are_not_stalled(self):
        path = self.task()
        for i in range(5):
            self.session(path, self.t0 + timedelta(days=i), note=f"slice {i}")
        self.assertEqual(stalls.detect(self.root, self.state, self.now), [])

    def test_five_nights_with_changing_content_not_detected(self):
        path = self.task()
        for i in range(5):
            self.session(path, self.t0 + timedelta(days=i, hours=1),
                         note=f"night {i}")
        self.assertEqual(stalls.detect(self.root, self.state, self.now), [])

    def test_below_threshold_not_stalled(self):
        path = self.task()
        for i in range(stalls.STALL_RUNS - 1):
            self.session(path, self.t0 + timedelta(days=i))
        self.assertEqual(stalls.detect(self.root, self.state, self.now), [])

    def test_three_spellings_count_as_one(self):
        path = self.task("rankr-discovery.md")
        spellings = ["tasks/rankr-discovery.md", "rankr-discovery.md",
                     "/Users/oscar/backlog/tasks/rankr-discovery.md"]
        for i in range(5):
            self.session(path, self.t0 + timedelta(minutes=50 * i),
                         note=f"s{i}", spelling=spellings[i % 3])
        self.assertEqual({r["task"] for r in stalls.orchestrate_runs(self.state)},
                         {"rankr-discovery.md"})
        found = stalls.detect(self.root, self.state, self.now)
        self.assertEqual([(f[0], f[1], f[2]) for f in found],
                         [("rankr-discovery.md", "storming", 5)])
        # The same night seen from a later day is history, not a storm.
        self.now += timedelta(days=2)
        self.assertEqual(stalls.detect(self.root, self.state, self.now), [])

    def test_duty_filler_parallel_never_judged(self):
        for name, extra in (("d.md", "duty: nightly\n"), ("f.md", "filler: true\n"),
                            ("s.md", "parallel: true\n")):
            path = self.task(name, TASK.replace("status: ready\n",
                                                "status: ready\n" + extra))
            for i in range(5):
                self.session(path, self.t0 + timedelta(days=i), code=1)
        self.assertEqual(stalls.detect(self.root, self.state, self.now), [])

    def test_same_nonzero_exit_is_storming(self):
        path = self.task()
        for i in range(stalls.STORM_SAME_EXIT):
            self.session(path, self.t0 + timedelta(days=i), note=f"{i}", code=1)
        self.assertEqual(stalls.detect(self.root, self.state, self.now)[0][1], "storming")

    def test_repeated_timeouts_are_not_storming(self):
        path = self.task()
        for i in range(stalls.STORM_SAME_EXIT):
            self.session(path, self.t0 + timedelta(days=i), note=f"{i}", code=124)
        self.assertEqual(stalls.detect(self.root, self.state, self.now), [])

    def test_block_writes_status_and_resets_count(self):
        path = self.task()
        for i in range(5):
            self.session(path, self.t0 + timedelta(days=i))
        now = self.t0 + timedelta(days=6)
        stalls.block_detected(self.root, self.state, now, tasks.set_status)
        text = path.read_text()
        self.assertIn("status: blocked", text)
        self.assertIn("stall detector: stalled", text)
        self.assertIn("5 runs counted", text)
        # The owner unblocks it: old runs no longer count.
        tasks.set_status(path, "ready", "- owner: retry\n")
        self.assertEqual(stalls.detect(self.root, self.state, self.now), [])
        self.assertEqual(stalls.history(self.state)[0]["task"], "a.md")

    def test_active_blocks_only_lists_still_blocked_tasks(self):
        path = self.task()
        for i in range(5):
            self.session(path, self.t0 + timedelta(days=i))
        stalls.block_detected(self.root, self.state, self.t0 + timedelta(days=6), tasks.set_status)
        self.assertEqual([h["task"] for h in stalls.active_blocks(self.root, self.state)], ["a.md"])
        # Unblocked by the owner: stays in the history, leaves the active list.
        tasks.set_status(path, "ready", "- owner: retry\n")
        self.assertEqual(stalls.active_blocks(self.root, self.state), [])
        self.assertEqual(len(stalls.history(self.state)), 1)


class GlobalStormTest(Base):
    def ledger(self, exits, sub="max"):
        d = self.state / sub
        d.mkdir(exist_ok=True)
        with open(d / "costs.jsonl", "a") as f:
            for i, code in enumerate(exits):
                row = {"ts": (self.t0 + timedelta(minutes=i)).isoformat(),
                       "mode": "orchestrate", "task": f"/x/tasks/t{i}.md",
                       "exit": code}
                if code == 0:
                    row.update(cost_usd=1.2, duration_ms=60000)
                f.write(json.dumps(row) + "\n")

    def trip(self):
        return stalls.trip_global(self.state, self.root / "orchestrator" / "PAUSED",
                                  self.root / "NEEDS-HUMAN.md")

    def test_five_failures_pause_once(self):
        (self.state / "launchd.gatekeeper.log").write_text("ok\nrun.sh: bad substitution\n")
        self.ledger([1] * 5)
        self.assertTrue(self.trip())
        self.assertTrue((self.root / "orchestrator" / "PAUSED").exists())
        needs = (self.root / "NEEDS-HUMAN.md").read_text()
        self.assertEqual(needs.count("- [ ]"), 1)
        self.assertIn("5 consecutive", needs)
        self.assertIn("run.sh: bad substitution", needs)
        # Owner deletes PAUSED: the same storm does not trip again.
        (self.root / "orchestrator" / "PAUSED").unlink()
        self.assertIsNone(self.trip())

    def test_four_failures_do_not_pause(self):
        self.ledger([1] * 4)
        self.assertIsNone(self.trip())
        self.assertFalse((self.root / "orchestrator" / "PAUSED").exists())

    def test_interleaved_success_does_not_pause(self):
        self.ledger([1, 1, 0, 1, 1, 1])
        self.assertIsNone(self.trip())
        self.assertFalse((self.root / "NEEDS-HUMAN.md").exists())

    def test_ledger_read_at_both_levels(self):
        self.ledger([0])
        with open(self.state / "costs.jsonl", "w") as f:
            f.write(json.dumps({"ts": "2026-09-01T00:00:00", "mode": "orchestrate",
                                "exit": 0}) + "\n")
        self.assertEqual(len(stalls.ledger_rows(self.state)), 2)

    def test_duplicate_rows_count_once(self):
        self.ledger([1] * 3)
        self.ledger([1] * 3, sub=".")
        self.assertEqual(len(stalls.ledger_rows(self.state)), 3)
        self.assertIsNone(self.trip())


if __name__ == "__main__":
    unittest.main()
