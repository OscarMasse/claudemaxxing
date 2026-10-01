import tempfile
import unittest
from pathlib import Path

from lib import stacks, tasks
from tests.test_tasks import write_task

URL_A = "https://github.com/o/r/pull/1"
URL_B = "https://github.com/o/r/pull/2"
URL_C = "https://github.com/o/r/pull/3"


class TestStacks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()
        self.prs = {URL_A: {"state": "OPEN", "head": "agent/a"},
                    URL_B: {"state": "OPEN", "head": "agent/b"}}
        self._orig = (tasks._pr_info, tasks._open_prs_on)
        tasks._pr_info = lambda url: self.prs.get(url)
        own = {"agent/a": [(URL_B, "agent/b")]}
        tasks._open_prs_on = lambda repo, head: own.get(head, [])
        self.task("a", URL_A, status="done")
        self.task("b", URL_B, status="done", prerequisites="a")
        self.task("solo", None, status="ready")

    def tearDown(self):
        tasks._pr_info, tasks._open_prs_on = self._orig
        self.tmp.cleanup()

    def task(self, name, url, **fm):
        write_task(self.root, f"{name}.md", project="side-projects",
                   delivery="pr", **fm)
        if url:
            p = self.root / "tasks" / f"{name}.md"
            p.write_text(p.read_text() + f"PR {url}\n")

    def test_collect_follows_the_chain_and_names_it_after_the_bottom(self):
        got, waits = stacks.collect(self.root)
        self.assertEqual(list(got), ["a"])
        self.assertEqual([l["task"] for l in got["a"]], ["a", "b"])
        self.assertEqual(got["a"][0]["pr"], URL_A)
        self.assertEqual(waits, [])

    def test_ready_when_all_done_open_green(self):
        out = stacks.render(stacks.collect(self.root),
                            lambda u: ("OPEN", "green"))
        self.assertIn("ready to merge", out[0])

    def test_red_layer_blocks_ready(self):
        out = stacks.render(stacks.collect(self.root),
                            lambda u: ("OPEN", "red" if u == URL_B else "green"))
        self.assertIn("not ready", out[0])

    def test_unfinished_layer_blocks_ready(self):
        write_task(self.root, "b.md", project="side-projects", delivery="pr",
                   status="in-progress", prerequisites="a")
        out = stacks.render(stacks.collect(self.root),
                            lambda u: ("OPEN", "green"))
        self.assertIn("not ready", out[0])

    def test_merged_bottom_layer_keeps_stack_ready(self):
        out = stacks.render(
            stacks.collect(self.root),
            lambda u: ("MERGED", "green") if u == URL_A else ("OPEN", "green"))
        self.assertIn("ready to merge", out[0])

    def test_waiting_task_is_listed_with_its_reason(self):
        self.prs[URL_C] = {"state": "OPEN", "head": "agent/c"}
        self.task("c", URL_C, status="done")
        self.task("d", None, status="ready", prerequisites="b c")
        _, waits = stacks.collect(self.root)
        self.assertEqual(waits, [("d", ["b", "c"], "diamond")])
        out = stacks.render(stacks.collect(self.root),
                            lambda u: ("OPEN", "green"))
        self.assertIn("- `d` waits for the merge of b, c (diamond)", out)

    def test_directive_names_the_base_and_gh_stack_link(self):
        self.task("e", None, status="in-progress", prerequisites="b")
        text = stacks.directive(self.root, "e")
        self.assertIn("`agent/b`", text)
        self.assertIn(f"gh stack link {URL_B}", text)
        self.assertIn("gh pr create --base agent/b", text)
        self.assertEqual(stacks.directive(self.root, "solo"), "")
