import json, os, tempfile, time, unittest
from datetime import datetime, timezone
from pathlib import Path
from lib import activity


def event_line(ts_epoch, type="assistant"):
    ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
    return json.dumps({
        "type": type,
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    })


def write_transcript(path, ts_epochs):
    path.write_text("\n".join(event_line(t) for t in ts_epochs) + "\n")


class TestActivity(unittest.TestCase):
    def test_idle_from_last_event_timestamp(self):
        with tempfile.TemporaryDirectory() as root:
            projects = Path(root)
            mine = projects / "-Users-alex-work-api"
            orch = projects / "-Users-alex-backlog"
            mine.mkdir(); orch.mkdir()
            now = time.time()
            write_transcript(mine / "s1.jsonl", [now - 7200, now - 3600])
            write_transcript(orch / "s2.jsonl", [now - 60])  # fresh, but excluded
            idle = activity.idle_minutes(projects, "-Users-alex-backlog", now)
            self.assertAlmostEqual(idle, 60.0, delta=0.5)

    def test_fresh_mtime_with_old_events_is_not_activity(self):
        # External processes (transcript compressors, indexers) rewrite session
        # files without appending events; the mtime bump must not reset idle.
        with tempfile.TemporaryDirectory() as root:
            projects = Path(root)
            mine = projects / "-Users-alex-projects-app"
            mine.mkdir()
            now = time.time()
            f = mine / "s1.jsonl"
            write_transcript(f, [now - 3600])
            os.utime(f, (now - 30, now - 30))  # rewritten 30s ago
            idle = activity.idle_minutes(projects, "-Users-alex-backlog", now)
            self.assertAlmostEqual(idle, 60.0, delta=0.5)

    def test_system_events_are_not_activity(self):
        # Claude Code appends housekeeping events (away_summary, turn_duration,
        # stop_hook_summary) to idle sessions; only user/assistant turns count.
        with tempfile.TemporaryDirectory() as root:
            projects = Path(root)
            mine = projects / "-Users-alex-projects-app"
            mine.mkdir()
            now = time.time()
            (mine / "s1.jsonl").write_text(
                event_line(now - 3600) + "\n"
                + event_line(now - 120, type="system") + "\n"
                + event_line(now - 60, type="summary") + "\n")
            idle = activity.idle_minutes(projects, "-Users-alex-backlog", now)
            self.assertAlmostEqual(idle, 60.0, delta=0.5)

    def test_file_with_only_system_events_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            projects = Path(root)
            mine = projects / "-Users-alex-projects-app"
            mine.mkdir()
            (mine / "s1.jsonl").write_text(
                event_line(time.time() - 60, type="system") + "\n")
            self.assertIsNone(
                activity.idle_minutes(projects, "-Users-alex-backlog", time.time()))

    def test_file_without_timestamps_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            projects = Path(root)
            mine = projects / "-Users-alex-projects-app"
            mine.mkdir()
            (mine / "s1.jsonl").write_text("{}\n")
            self.assertIsNone(
                activity.idle_minutes(projects, "-Users-alex-backlog", time.time()))

    def test_trailing_garbage_falls_back_to_previous_line(self):
        # A partial in-flight write must not hide the previous valid event.
        with tempfile.TemporaryDirectory() as root:
            projects = Path(root)
            mine = projects / "-Users-alex-projects-app"
            mine.mkdir()
            now = time.time()
            f = mine / "s1.jsonl"
            f.write_text(event_line(now - 1800) + "\n" + '{"type": "assis')
            idle = activity.idle_minutes(projects, "-Users-alex-backlog", now)
            self.assertAlmostEqual(idle, 30.0, delta=0.5)

    def test_newest_file_across_projects_wins(self):
        with tempfile.TemporaryDirectory() as root:
            projects = Path(root)
            a = projects / "-Users-alex-projects-app"
            b = projects / "-Users-alex-notes"
            a.mkdir(); b.mkdir()
            now = time.time()
            write_transcript(a / "s1.jsonl", [now - 7200])
            write_transcript(b / "s2.jsonl", [now - 600])
            idle = activity.idle_minutes(projects, "-Users-alex-backlog", now)
            self.assertAlmostEqual(idle, 10.0, delta=0.5)

    def test_no_files_returns_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(activity.idle_minutes(Path(root), "x", time.time()))


if __name__ == "__main__":
    unittest.main()
