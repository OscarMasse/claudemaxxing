import tempfile
import unittest
from pathlib import Path

from lib import stacks
from tests.test_tasks import write_task

URL_A = "https://github.com/o/r/pull/1"
URL_B = "https://github.com/o/r/pull/2"


class TestStacks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()
        write_task(self.root, "a.md", stack="s", status="done")
        write_task(self.root, "b.md", stack="s", status="done",
                   prerequisites="a")
        write_task(self.root, "solo.md", status="ready")
        (self.root / "tasks" / "a.md").write_text(
            (self.root / "tasks" / "a.md").read_text() + f"PR {URL_A}\n")
        (self.root / "tasks" / "b.md").write_text(
            (self.root / "tasks" / "b.md").read_text() + f"PR {URL_B}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_orders_layers_and_ignores_unstacked(self):
        got = stacks.collect(self.root)
        self.assertEqual(list(got), ["s"])
        self.assertEqual([l["task"] for l in got["s"]], ["a", "b"])
        self.assertEqual(got["s"][0]["pr"], URL_A)

    def test_ready_when_all_done_open_green(self):
        out = stacks.render(stacks.collect(self.root),
                            lambda u: ("OPEN", "green"))
        self.assertIn("ready to merge", out[0])

    def test_red_layer_blocks_ready(self):
        out = stacks.render(stacks.collect(self.root),
                            lambda u: ("OPEN", "red" if u == URL_B else "green"))
        self.assertIn("not ready", out[0])

    def test_unfinished_layer_blocks_ready(self):
        write_task(self.root, "b.md", stack="s", status="in-progress",
                   prerequisites="a")
        out = stacks.render(stacks.collect(self.root),
                            lambda u: ("OPEN", "green"))
        self.assertIn("not ready", out[0])
