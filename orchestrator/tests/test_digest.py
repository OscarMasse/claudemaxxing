import unittest
from datetime import date, datetime

from lib import digest


class TestDigestDay(unittest.TestCase):
    def test_before_digest_time_is_today(self):
        now = datetime(2026, 9, 4, 7, 36)
        self.assertEqual(digest.digest_day(now, "07:37"), date(2026, 9, 4))

    def test_at_digest_time_is_tomorrow(self):
        now = datetime(2026, 9, 4, 7, 37)
        self.assertEqual(digest.digest_day(now, "07:37"), date(2026, 9, 5))

    def test_after_digest_time_is_tomorrow(self):
        now = datetime(2026, 9, 4, 7, 38)
        self.assertEqual(digest.digest_day(now, "07:37"), date(2026, 9, 5))

    def test_well_before_is_today(self):
        now = datetime(2026, 9, 4, 3, 0)
        self.assertEqual(digest.digest_day(now, "07:37"), date(2026, 9, 4))

    def test_well_after_is_tomorrow(self):
        now = datetime(2026, 9, 4, 23, 0)
        self.assertEqual(digest.digest_day(now, "07:37"), date(2026, 9, 5))

    def test_midnight_crossing_rolls_over_the_month(self):
        now = datetime(2026, 9, 30, 23, 59)
        self.assertEqual(digest.digest_day(now, "07:37"), date(2026, 10, 1))

    def test_midnight_itself_is_before_any_reasonable_digest_time(self):
        now = datetime(2026, 9, 4, 0, 0)
        self.assertEqual(digest.digest_day(now, "07:37"), date(2026, 9, 4))


class TestDigestFile(unittest.TestCase):
    def test_path_under_backlog_root(self):
        now = datetime(2026, 9, 4, 3, 0)
        path = digest.digest_file(now, "07:37", "/tmp/some-backlog-root")
        self.assertEqual(str(path), "/tmp/some-backlog-root/digests/2026-09-04.md")

    def test_path_rolls_to_tomorrow_after_digest_time(self):
        now = datetime(2026, 9, 4, 8, 0)
        path = digest.digest_file(now, "07:37", "/tmp/some-backlog-root")
        self.assertEqual(str(path), "/tmp/some-backlog-root/digests/2026-09-05.md")


if __name__ == "__main__":
    unittest.main()
