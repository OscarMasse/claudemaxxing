"""Render a session prompt template.

Usage:
  prompt.py render <template> KEY=VALUE ...
      Print the template with every {{KEY}} replaced by VALUE. Fails if a
      placeholder is left unfilled, so a template and run.sh cannot drift.
  prompt.py times <slice_min>
      Print "<start>\\t<deadline>" in local time, the slice's wall-clock bounds.

This replaced a chain of `sed` substitutions in run.sh: a value containing the
sed delimiter broke the prompt, and a new placeholder could be forgotten
without anything noticing.

The slice bounds exist because a headless session has no clock unless it runs
`date`. On 2026-09-27 five sessions told "about N minutes" each finished one
item, guessed the slice was over and exited after 2-8 minutes of 7-33.
"""
import re
import sys
from datetime import datetime, timedelta

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def render(template, values):
    missing = sorted(set(_PLACEHOLDER.findall(template)) - set(values))
    if missing:
        raise KeyError(f"unfilled placeholders: {', '.join(missing)}")
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def slice_times(now, slice_min):
    """(start, deadline) of a slice starting `now`, as local wall-clock text."""
    now = now.astimezone()
    fmt = "%Y-%m-%d %H:%M %Z"
    return (now.strftime(fmt),
            (now + timedelta(minutes=int(slice_min))).strftime(fmt))


def main(argv):
    if len(argv) >= 2 and argv[0] == "render":
        values = dict(a.split("=", 1) for a in argv[2:])
        with open(argv[1]) as f:
            sys.stdout.write(render(f.read(), values))
        return 0
    if len(argv) == 2 and argv[0] == "times":
        print("\t".join(slice_times(datetime.now(), argv[1])))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
