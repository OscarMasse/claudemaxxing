import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import transcripts

T0 = datetime(2026, 9, 8, 23, 20, tzinfo=timezone.utc)


def entry(ts, model, msg_id="m1", request_id="r1", **tokens):
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    usage.update(tokens)
    return {"timestamp": ts.isoformat().replace("+00:00", "Z"),
            "requestId": request_id,
            "message": {"id": msg_id, "model": model, "usage": usage}}


class TestFamily(unittest.TestCase):
    def test_known_prefixes(self):
        self.assertEqual(transcripts.family("claude-fable-5-1"), "fable")
        self.assertEqual(transcripts.family("claude-opus-5"), "opus")
        self.assertEqual(transcripts.family("claude-sonnet-5"), "sonnet")
        self.assertEqual(transcripts.family("claude-haiku-4-5-20251001"), "haiku")

    def test_unknown_and_empty(self):
        self.assertIsNone(transcripts.family("gpt-9"))
        self.assertIsNone(transcripts.family(None))

    def test_point_release_needs_no_new_entry(self):
        self.assertEqual(transcripts.family("claude-fable-9-3-20301231"), "fable")


class TestEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.proj = self.dir / "projects" / "slug"
        self.proj.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, rows):
        (self.proj / name).write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    def test_sums_the_four_token_components(self):
        self.write("a.jsonl", [entry(T0, "claude-sonnet-5", input_tokens=1,
                                     output_tokens=2,
                                     cache_read_input_tokens=4,
                                     cache_creation_input_tokens=8)])
        rows = transcripts.entries(self.dir)
        self.assertEqual(rows, [(T0, "claude-sonnet-5", 15)])

    def test_replayed_entry_is_counted_once(self):
        # A resumed or forked session rewrites earlier entries into the new
        # file with a fresh uuid but the same (message id, request id): one
        # billed message, however many copies are on disk.
        e = entry(T0, "claude-opus-5", input_tokens=100)
        self.write("a.jsonl", [e])
        self.write("b.jsonl", [dict(e, uuid="other")])
        rows = transcripts.entries(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 100)

    def test_distinct_requests_are_both_counted(self):
        self.write("a.jsonl", [
            entry(T0, "claude-opus-5", msg_id="m1", request_id="r1",
                  input_tokens=1),
            entry(T0, "claude-opus-5", msg_id="m1", request_id="r2",
                  input_tokens=1),
        ])
        self.assertEqual(len(transcripts.entries(self.dir)), 2)

    def test_synthetic_entries_are_not_consumption(self):
        # Claude Code's own notices (API errors, interrupts) carry this id and
        # zero tokens; reporting them as an unrecognized model is noise.
        self.write("a.jsonl", [entry(T0, transcripts.SYNTHETIC),
                               entry(T0, "claude-sonnet-5", msg_id="m2",
                                     input_tokens=5)])
        rows = transcripts.entries(self.dir)
        self.assertEqual(rows, [(T0, "claude-sonnet-5", 5)])

    def test_partial_line_does_not_take_the_decider_down(self):
        (self.proj / "a.jsonl").write_text(
            json.dumps(entry(T0, "claude-sonnet-5", input_tokens=3)) + "\n"
            + '{"timestamp": "2026-09-08T23')
        rows = transcripts.entries(self.dir)
        self.assertEqual(len(rows), 1)

    def test_entries_without_usage_are_skipped(self):
        self.write("a.jsonl", [{"timestamp": T0.isoformat(),
                                "message": {"role": "user"}}])
        self.assertEqual(transcripts.entries(self.dir), [])

    def test_since_filters(self):
        self.write("a.jsonl", [
            entry(T0 - timedelta(days=3), "claude-sonnet-5", msg_id="old",
                  input_tokens=7),
            entry(T0, "claude-sonnet-5", msg_id="new", input_tokens=1),
        ])
        rows = transcripts.entries(self.dir, since=T0 - timedelta(hours=1))
        self.assertEqual([r[2] for r in rows], [1])

    def test_missing_config_dir_is_empty_not_an_error(self):
        self.assertEqual(transcripts.entries(self.dir / "nope"), [])

    def test_result_is_sorted_across_files(self):
        self.write("b.jsonl", [entry(T0 + timedelta(minutes=5), "claude-opus-5",
                                     msg_id="late")])
        self.write("a.jsonl", [entry(T0, "claude-opus-5", msg_id="early")])
        rows = transcripts.entries(self.dir)
        self.assertEqual([r[0] for r in rows],
                         [T0, T0 + timedelta(minutes=5)])


class TestBlocks(unittest.TestCase):
    def test_window_starts_at_the_first_token_not_the_hour(self):
        # The account resets 5h after the first token (23:20 -> 04:20), which
        # is what /usage shows. ccusage floored this to 23:00 and gave away
        # 20 minutes of window.
        rows = [(T0, "claude-sonnet-5", 10)]
        b = transcripts.blocks(rows)[0]
        self.assertEqual(b["start"], T0)
        self.assertEqual(b["end"], T0 + timedelta(hours=5))

    def test_entry_at_the_boundary_opens_the_next_window(self):
        rows = [(T0, "claude-sonnet-5", 1),
                (T0 + timedelta(hours=4, minutes=59), "claude-sonnet-5", 2),
                (T0 + timedelta(hours=5), "claude-sonnet-5", 4)]
        out = transcripts.blocks(rows)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["tokens"], 3)
        self.assertEqual(out[1]["tokens"], 4)
        self.assertEqual(out[1]["start"], T0 + timedelta(hours=5))

    def test_per_model_breakdown(self):
        rows = [(T0, "claude-fable-5-1", 10),
                (T0, "claude-sonnet-5", 3),
                (T0, "claude-fable-5-1", 5)]
        b = transcripts.blocks(rows)[0]
        self.assertEqual(b["by_model"],
                         {"claude-fable-5-1": 15, "claude-sonnet-5": 3})

    def test_no_rows_no_blocks(self):
        self.assertEqual(transcripts.blocks([]), [])


class TestByFamily(unittest.TestCase):
    def test_groups_and_reports_unknown(self):
        unknown = []
        out = transcripts.by_family(
            {"claude-fable-5": 1, "claude-fable-5-1": 2, "weird-model": 4},
            unknown)
        self.assertEqual(out, {"fable": 3, "unknown": 4})
        self.assertEqual(unknown, ["weird-model"])

    def test_unknown_reported_once(self):
        unknown = ["weird-model"]
        transcripts.by_family({"weird-model": 1}, unknown)
        self.assertEqual(unknown, ["weird-model"])


class TestSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        proj = self.dir / "projects" / "slug"
        proj.mkdir(parents=True)
        rows = [
            # Before `since`: not part of the week.
            entry(T0 - timedelta(days=4), "claude-opus-5", msg_id="pre",
                  input_tokens=1000),
            # A closed window.
            entry(T0, "claude-sonnet-5", msg_id="a", input_tokens=10),
            # The window that is open at `now`.
            entry(T0 + timedelta(hours=6), "claude-fable-5-1", msg_id="b",
                  input_tokens=20),
            entry(T0 + timedelta(hours=6, minutes=1), "claude-sonnet-5",
                  msg_id="c", input_tokens=5),
        ]
        (proj / "a.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
        self.since = T0 - timedelta(hours=1)
        self.now = T0 + timedelta(hours=7)

    def tearDown(self):
        self.tmp.cleanup()

    def test_week_totals_and_active_block(self):
        s = transcripts.summary(self.dir, self.since, self.now)
        self.assertEqual(s["week_tokens"], 35)
        self.assertEqual(s["week_by_family"],
                         {"sonnet": 15, "fable": 20})
        self.assertEqual(s["block"]["start"], T0 + timedelta(hours=6))
        self.assertEqual(s["block"]["tokens"], 25)
        self.assertEqual(s["block"]["by_family"], {"fable": 20, "sonnet": 5})
        self.assertTrue(s["block"]["active"])
        self.assertEqual(s["unknown_models"], [])

    def test_no_open_window(self):
        s = transcripts.summary(self.dir, self.since,
                                T0 + timedelta(hours=20))
        self.assertIsNone(s["block"])
        self.assertEqual(s["week_tokens"], 35)


if __name__ == "__main__":
    unittest.main()
