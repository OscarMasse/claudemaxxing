import tempfile
import unittest
from pathlib import Path
from lib import tasks


def proj(name, account, priority=100, local_only_default=False):
    return {"name": name, "account": account, "dirs": [],
            "priority": priority, "local_only_default": local_only_default}


PROJECTS = {
    "side-projects": proj("side-projects", "personal", priority=10),
    "life": proj("life", "personal", priority=50, local_only_default=True),
    "work": proj("work", "employer", priority=10, local_only_default=True),
}


def write_task(root, name, **fm):
    lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", ""]
    (root / "tasks" / name).write_text("\n".join(lines))


class TestPick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def pick(self, account="personal", max_model="sonnet", projects=PROJECTS):
        return tasks.pick(self.root, projects, account, max_model)

    def test_picks_highest_priority_oldest(self):
        write_task(self.root, "a.md", project="side-projects", status="ready",
                   priority="low", created="2026-08-01")
        write_task(self.root, "b.md", project="side-projects", status="ready",
                   priority="high", created="2026-08-05")
        write_task(self.root, "c.md", project="side-projects", status="ready",
                   priority="high", created="2026-08-02")
        picked = self.pick()
        self.assertTrue(picked["path"].endswith("c.md"))
        self.assertEqual(picked["model"], "sonnet")
        self.assertEqual(picked["effort"], "low")
        self.assertEqual(picked["project"], "side-projects")

    def test_project_routes_to_account(self):
        write_task(self.root, "w.md", project="work", status="ready", priority="high")
        write_task(self.root, "p.md", project="side-projects", status="ready",
                   priority="low")
        picked = self.pick(account="employer")
        self.assertTrue(picked["path"].endswith("w.md"))
        picked = self.pick(account="personal")
        self.assertTrue(picked["path"].endswith("p.md"))

    def test_other_accounts_tasks_never_fill_slots(self):
        write_task(self.root, "w.md", project="work", status="ready", priority="high")
        write_task(self.root, "p.md", project="side-projects", status="ready",
                   priority="low")
        picked = tasks.pick_multi(self.root, PROJECTS, "personal", "sonnet", 3)
        self.assertEqual(len(picked), 1)
        self.assertTrue(picked[0]["path"].endswith("p.md"))

    def test_unknown_project_ineligible(self):
        write_task(self.root, "x.md", project="nope", status="ready", priority="high")
        self.assertIsNone(self.pick())
        self.assertIsNone(self.pick(account="employer"))

    def test_no_project_key_routes_to_default_project(self):
        write_task(self.root, "x.md", status="ready", priority="high")
        self.assertIsNone(self.pick())  # no project named "default" registered
        legacy = {"default": proj("default", "default")}
        picked = self.pick(account="default", projects=legacy)
        self.assertTrue(picked["path"].endswith("x.md"))

    def test_project_priority_orders_before_task_priority(self):
        # life has project priority 50, side-projects 10: a low-priority
        # side-projects task still beats a high-priority life task.
        write_task(self.root, "l.md", project="life", status="ready",
                   priority="high", created="2026-08-01")
        write_task(self.root, "s.md", project="side-projects", status="ready",
                   priority="low", created="2026-08-05")
        picked = self.pick()
        self.assertTrue(picked["path"].endswith("s.md"))

    def test_local_only_inherits_project_default(self):
        write_task(self.root, "l.md", project="life", status="ready", priority="high")
        self.assertIs(self.pick()["local_only"], True)

    def test_local_only_task_overrides_project_default(self):
        write_task(self.root, "l.md", project="life", status="ready",
                   priority="high", local_only="false")
        self.assertIs(self.pick()["local_only"], False)
        write_task(self.root, "l.md", project="side-projects", status="ready",
                   priority="high", local_only="true")
        self.assertIs(self.pick()["local_only"], True)

    def test_model_floor_gated_by_max_model(self):
        write_task(self.root, "big.md", project="side-projects", status="ready",
                   priority="high", model="opus", created="2026-08-01")
        write_task(self.root, "small.md", project="side-projects", status="ready",
                   priority="low", created="2026-08-02")
        day = self.pick(max_model="sonnet")
        self.assertTrue(day["path"].endswith("small.md"))  # opus floor skipped
        night = self.pick(max_model="opus")
        self.assertTrue(night["path"].endswith("big.md"))
        self.assertEqual(night["model"], "opus")

    def test_fable_floor_only_under_fable_ceiling(self):
        write_task(self.root, "f.md", project="side-projects", status="ready",
                   priority="high", model="fable")
        self.assertIsNone(self.pick(max_model="opus"))  # never downgraded
        picked = self.pick(max_model="fable")
        self.assertEqual(picked["model"], "fable")

    def test_non_ready_ignored(self):
        write_task(self.root, "x.md", project="side-projects", status="blocked",
                   priority="high")
        self.assertIsNone(self.pick(max_model="fable"))

    def test_pick_multi_distinct_then_parallel_fill(self):
        write_task(self.root, "a.md", project="side-projects", status="ready",
                   priority="high", created="2026-08-01", parallel="true")
        write_task(self.root, "b.md", project="side-projects", status="ready",
                   priority="low", created="2026-08-02")
        picked = tasks.pick_multi(self.root, PROJECTS, "personal", "sonnet", 3)
        paths = [p["path"].rsplit("/", 1)[1] for p in picked]
        self.assertEqual(paths, ["a.md", "b.md", "a.md"])  # distinct first, then fill

    def test_pick_multi_no_fill_without_parallel_flag(self):
        write_task(self.root, "a.md", project="side-projects", status="ready",
                   priority="high", created="2026-08-01")
        picked = tasks.pick_multi(self.root, PROJECTS, "personal", "sonnet", 3)
        self.assertEqual(len(picked), 1)

    def test_unmet_prerequisite_excludes_task(self):
        write_task(self.root, "dep.md", project="side-projects", status="ready",
                   priority="high")
        write_task(self.root, "main.md", project="side-projects", status="ready",
                   priority="high", prerequisites="dep")
        self.assertTrue(self.pick()["path"].endswith("dep.md"))

    def test_met_prerequisite_includes_task(self):
        write_task(self.root, "dep.md", project="side-projects", status="done",
                   priority="high")
        write_task(self.root, "main.md", project="side-projects", status="ready",
                   priority="high", prerequisites="dep")
        self.assertTrue(self.pick()["path"].endswith("main.md"))

    def test_missing_prerequisite_file_excludes_task(self):
        write_task(self.root, "main.md", project="side-projects", status="ready",
                   priority="high", prerequisites="ghost")
        self.assertIsNone(self.pick())

    def test_prerequisite_md_suffix_optional(self):
        write_task(self.root, "dep.md", project="side-projects", status="done",
                   priority="high")
        write_task(self.root, "main.md", project="side-projects", status="ready",
                   priority="high", prerequisites="dep.md")
        self.assertTrue(self.pick()["path"].endswith("main.md"))

    def test_multiple_prerequisites_all_must_be_done(self):
        write_task(self.root, "dep1.md", project="side-projects", status="done",
                   priority="high")
        write_task(self.root, "dep2.md", project="side-projects", status="ready",
                   priority="high")
        write_task(self.root, "main.md", project="side-projects", status="ready",
                   priority="high", prerequisites="dep1 dep2")
        picked = self.pick()
        self.assertTrue(picked["path"].endswith("dep2.md"))  # main still blocked
        # once dep2 is also done, main becomes eligible
        write_task(self.root, "dep2.md", project="side-projects", status="done",
                   priority="high")
        self.assertTrue(self.pick()["path"].endswith("main.md"))

    def test_blocked_reports_unmet_prerequisites(self):
        write_task(self.root, "dep1.md", project="side-projects", status="ready",
                   priority="high")
        write_task(self.root, "main.md", project="side-projects", status="ready",
                   priority="high", prerequisites="dep1 ghost")
        blocked = tasks.blocked(self.root, PROJECTS, "personal")
        self.assertEqual(len(blocked), 1)
        name, unmet = blocked[0]
        self.assertEqual(name, "main.md")
        self.assertEqual(set(unmet), {"dep1", "ghost"})

    def test_blocked_empty_when_no_prerequisites_unmet(self):
        write_task(self.root, "a.md", project="side-projects", status="ready",
                   priority="high")
        self.assertEqual(tasks.blocked(self.root, PROJECTS, "personal"), [])


if __name__ == "__main__":
    unittest.main()
