"""Digest-day bucketing: which day's digest file a run's journal entry belongs to.

A run journals into the digest file that will be read at the NEXT digest_time,
not necessarily today's file: a run finishing after this morning's digest was
already curated must land in tomorrow's file, or the owner never sees it.

Rule: local time strictly before digest_time -> TODAY's digest (still ahead of
today's curation); local time at or after digest_time -> TOMORROW's digest.
"""
from datetime import timedelta
from pathlib import Path


def digest_day(now, digest_time):
    """The digest day (a date) a run at `now` belongs to.

    now: a datetime (only .hour, .minute, .date() are used, so both naive and
         aware datetimes work).
    digest_time: "HH:MM" string, the daily digest's scheduled time.
    """
    hh, mm = digest_time.split(":")
    boundary = (int(hh), int(mm))
    current = (now.hour, now.minute)
    if current < boundary:
        return now.date()
    return now.date() + timedelta(days=1)


def digest_file(now, digest_time, backlog_root):
    """Path to the digest file for the day the run at `now` belongs to."""
    day = digest_day(now, digest_time)
    return Path(backlog_root) / "digests" / f"{day.isoformat()}.md"
