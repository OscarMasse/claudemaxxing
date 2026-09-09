import os
import tempfile
import time
import unittest
from pathlib import Path
from lib import tasks


def proj(name, account, priority=100, local_only_default=False):
    return {"name": name, "account": account, "dirs": [],
            "priority": priority, "local_only_default": local_only_default}


PROJECTS = {
    "side-projects": proj("side-projects", "personal", priority=10),
    "life": proj("life", "personal", priority=50),
    "work": proj("work", "employer", priority=10, local_only_default=True),
}


def write_task(root, name, **fm):
    """`delivery` defaults to `branch` here because it is mandatory in the
    files and almost every test is about something else. Pass
    `delivery=None` to write a task without the key, and any explicit value
    to exercise the contract itself."""
    fm.setdefault("delivery", "branch")
    lines = (["---"]
             + [f"{k}: {v}" for k, v in fm.items() if v is not None]
             + ["---", ""])
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
        write_task(self.root, "w.md", project="work", status="ready", delivery="local", priority="high")
        write_task(self.root, "p.md", project="side-projects", status="ready",
                   priority="low")
        picked = self.pick(account="employer")
        self.assertTrue(picked["path"].endswith("w.md"))
        picked = self.pick(account="personal")
        self.assertTrue(picked["path"].endswith("p.md"))

    def test_other_accounts_tasks_never_fill_slots(self):
        write_task(self.root, "w.md", project="work", status="ready", delivery="local", priority="high")
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
                   priority="high", delivery="local", created="2026-08-01")
        write_task(self.root, "s.md", project="side-projects", status="ready",
                   priority="low", created="2026-08-05")
        picked = self.pick()
        self.assertTrue(picked["path"].endswith("s.md"))

    def test_each_delivery_value_is_carried_to_the_session(self):
        for value in ("branch", "pr"):
            write_task(self.root, "d.md", project="side-projects",
                       status="ready", priority="high", delivery=value)
            picked = self.pick()
            self.assertEqual(picked["delivery"], value)
            self.assertIs(picked["local_only"], False)

    def test_delivery_local_is_what_drives_the_local_only_rails(self):
        # `local_only` is no longer declared: it is exactly `delivery: local`,
        # so the two can never contradict each other.
        write_task(self.root, "d.md", project="side-projects", status="ready",
                   priority="high", delivery="local")
        picked = self.pick()
        self.assertEqual(picked["delivery"], "local")
        self.assertIs(picked["local_only"], True)

    def test_missing_delivery_makes_the_task_unschedulable(self):
        write_task(self.root, "ghost.md", project="side-projects",
                   status="ready", priority="high", delivery=None)
        write_task(self.root, "ok.md", project="side-projects", status="ready",
                   priority="low")
        self.assertTrue(self.pick()["path"].endswith("ok.md"))

    def test_unknown_delivery_value_makes_the_task_unschedulable(self):
        write_task(self.root, "ghost.md", project="side-projects",
                   status="ready", priority="high", delivery="PR")
        self.assertIsNone(self.pick())

    def test_a_local_only_project_refuses_a_task_that_would_leave_the_machine(self):
        # The floor is not a downgrade: the task is not quietly run as `local`,
        # it does not run at all until the contradiction is fixed.
        for value in ("pr", "branch"):
            write_task(self.root, "w.md", project="work", status="ready",
                       priority="high", delivery=value)
            self.assertIsNone(self.pick(account="employer"))
        write_task(self.root, "w.md", project="work", status="ready",
                   priority="high", delivery="local")
        self.assertTrue(self.pick(account="employer")["path"].endswith("w.md"))

    def test_a_leftover_local_only_key_is_not_obeyed_but_refused(self):
        write_task(self.root, "old.md", project="side-projects", status="ready",
                   priority="high", delivery="pr", local_only="true")
        self.assertIsNone(self.pick())

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

    def test_blocker_inherits_its_dependent_priority(self):
        # The whole point: a low blocker under a high task must not sit behind
        # unrelated medium work, or the high task can never become eligible.
        write_task(self.root, "blocker.md", project="side-projects",
                   status="ready", priority="low", created="2026-08-05")
        write_task(self.root, "urgent.md", project="side-projects",
                   status="ready", priority="high", created="2026-08-01",
                   prerequisites="blocker")
        write_task(self.root, "unrelated.md", project="side-projects",
                   status="ready", priority="medium", created="2026-08-01")
        self.assertTrue(self.pick()["path"].endswith("blocker.md"))

    def test_priority_inheritance_is_transitive(self):
        write_task(self.root, "root.md", project="side-projects", status="ready",
                   priority="low", created="2026-08-05")
        write_task(self.root, "middle.md", project="side-projects",
                   status="ready", priority="low", created="2026-08-04",
                   prerequisites="root")
        write_task(self.root, "urgent.md", project="side-projects",
                   status="ready", priority="high", created="2026-08-01",
                   prerequisites="middle")
        write_task(self.root, "unrelated.md", project="side-projects",
                   status="ready", priority="medium", created="2026-08-01")
        self.assertTrue(self.pick()["path"].endswith("root.md"))

    def test_done_dependent_does_not_keep_promoting_its_prerequisite(self):
        write_task(self.root, "blocker.md", project="side-projects",
                   status="ready", priority="low", created="2026-08-05")
        write_task(self.root, "urgent.md", project="side-projects",
                   status="done", priority="high", created="2026-08-01",
                   prerequisites="blocker")
        write_task(self.root, "unrelated.md", project="side-projects",
                   status="ready", priority="medium", created="2026-08-01")
        self.assertTrue(self.pick()["path"].endswith("unrelated.md"))

    def test_inheritance_never_demotes_a_blocker(self):
        write_task(self.root, "blocker.md", project="side-projects",
                   status="ready", priority="high", created="2026-08-05")
        write_task(self.root, "lazy.md", project="side-projects", status="ready",
                   priority="low", created="2026-08-01", prerequisites="blocker")
        write_task(self.root, "unrelated.md", project="side-projects",
                   status="ready", priority="medium", created="2026-08-01")
        self.assertTrue(self.pick()["path"].endswith("blocker.md"))

    def test_prerequisite_cycle_does_not_hang(self):
        # A cycle is a misconfiguration, but it must not wedge the gatekeeper.
        write_task(self.root, "a.md", project="side-projects", status="ready",
                   priority="high", created="2026-08-01", prerequisites="b")
        write_task(self.root, "b.md", project="side-projects", status="ready",
                   priority="low", created="2026-08-02", prerequisites="a")
        write_task(self.root, "ok.md", project="side-projects", status="ready",
                   priority="low", created="2026-08-03")
        # Both cycle members stay ineligible (unmet prerequisites); the run
        # terminates and the unblocked task is picked.
        self.assertTrue(self.pick()["path"].endswith("ok.md"))

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

    def test_orphaned_reports_unknown_project(self):
        write_task(self.root, "ghost.md", project="typo", status="ready")
        write_task(self.root, "ok.md", project="side-projects", status="ready")
        # It is unschedulable AND invisible to every account's blocked report.
        self.assertTrue(self.pick()["path"].endswith("ok.md"))
        self.assertEqual(tasks.blocked(self.root, PROJECTS, "personal"), [])
        self.assertEqual(tasks.orphaned(self.root, PROJECTS),
                         [("ghost.md", "typo")])

    def test_orphaned_ignores_non_ready_tasks(self):
        write_task(self.root, "ghost.md", project="typo", status="inbox")
        self.assertEqual(tasks.orphaned(self.root, PROJECTS), [])

    def test_orphaned_reports_missing_default_project(self):
        # A task with no `project:` key routes to "default", which this config
        # does not declare: it is orphaned, not silently dropped.
        write_task(self.root, "legacy.md", status="ready")
        self.assertEqual(tasks.orphaned(self.root, PROJECTS),
                         [("legacy.md", "default")])

    def test_orphaned_empty_when_every_project_resolves(self):
        write_task(self.root, "a.md", project="side-projects", status="ready")
        write_task(self.root, "w.md", project="work", status="ready", delivery="local")
        self.assertEqual(tasks.orphaned(self.root, PROJECTS), [])

    def test_unknown_model_makes_the_task_unschedulable_and_reported(self):
        # The regression this guards: `claude-fable-5` (a CLI model id, not an
        # engine model name) used to be rewritten to sonnet, so tasks written
        # for Fable ran on Sonnet and every log line said "sonnet".
        write_task(self.root, "ghost.md", project="side-projects",
                   status="ready", priority="high", model="claude-fable-5")
        write_task(self.root, "ok.md", project="side-projects",
                   status="ready", priority="low")
        self.assertTrue(self.pick(max_model="fable")["path"].endswith("ok.md"))
        self.assertEqual(tasks.misconfigured(self.root, PROJECTS),
                         [("ghost.md", "model=claude-fable-5")])

    def test_absent_delivery_is_reported_not_guessed(self):
        write_task(self.root, "ghost.md", project="side-projects",
                   status="ready", delivery=None)
        self.assertEqual(tasks.misconfigured(self.root, PROJECTS),
                         [("ghost.md", "delivery=<missing>")])

    def test_unknown_delivery_value_is_reported_with_its_value(self):
        write_task(self.root, "ghost.md", project="side-projects",
                   status="ready", delivery="pull-request")
        self.assertEqual(tasks.misconfigured(self.root, PROJECTS),
                         [("ghost.md", "delivery=pull-request")])

    def test_delivery_breaching_the_local_only_floor_is_reported(self):
        write_task(self.root, "leak.md", project="work", status="ready",
                   delivery="pr")
        self.assertEqual(tasks.misconfigured(self.root, PROJECTS),
                         [("leak.md", "delivery=pr in local-only project work")])

    def test_a_leftover_local_only_key_is_reported(self):
        write_task(self.root, "old.md", project="side-projects", status="ready",
                   delivery="pr", local_only="true")
        self.assertEqual(
            tasks.misconfigured(self.root, PROJECTS),
            [("old.md", "local_only= (replaced by delivery:, remove the key)")])

    def test_only_ready_tasks_are_reported(self):
        # An inbox task without `delivery:` is not misconfigured, it is simply
        # not written yet; reporting it would make the list permanent noise.
        write_task(self.root, "later.md", project="side-projects",
                   status="inbox", delivery=None)
        self.assertEqual(tasks.misconfigured(self.root, PROJECTS), [])

    def test_known_models_are_not_reported_as_misconfigured(self):
        for i, model in enumerate(("sonnet", "opus", "fable")):
            write_task(self.root, f"m{i}.md", project="side-projects",
                       status="ready", model=model)
        write_task(self.root, "none.md", project="side-projects", status="ready")
        self.assertEqual(tasks.misconfigured(self.root, PROJECTS), [])


class TestSchedulingClasses(unittest.TestCase):
    """Duties (mandatory) and fillers (surplus only) versus the priority queue."""

    KEYS = {"nightly": "2026-08-12", "weekly": "2026-08-06"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()
        # A high-priority queue task: whatever a duty does, it must beat this.
        write_task(self.root, "queue.md", project="side-projects", status="ready",
                   priority="high", created="2026-08-01")

    def tearDown(self):
        self.tmp.cleanup()

    def order(self, count=3, done=None):
        return tasks.launch_order(self.root, PROJECTS, "personal", "sonnet", count,
                                  done=done or {}, period_keys=self.KEYS)

    def names(self, picked):
        return [Path(t["path"]).name for t in picked]

    def path(self, name):
        return str(self.root / "tasks" / name)

    def test_duty_runs_before_a_higher_priority_queue_task(self):
        write_task(self.root, "sync.md", project="life", status="ready",
                   priority="low", created="2026-09-01", duty="nightly")
        self.assertEqual(self.names(self.order()), ["sync.md", "queue.md"])

    def test_duty_is_not_relaunched_within_the_same_period(self):
        write_task(self.root, "sync.md", project="life", status="ready",
                   duty="nightly", created="2026-09-01")
        done = {self.path("sync.md"): "2026-08-12"}
        self.assertEqual(self.names(self.order(done=done)), ["queue.md"])

    def test_duty_is_due_again_in_the_next_period(self):
        write_task(self.root, "sync.md", project="life", status="ready",
                   duty="nightly", created="2026-09-01")
        done = {self.path("sync.md"): "2026-08-11"}
        self.assertIn("sync.md", self.names(self.order(done=done)))

    def test_nightly_duty_not_schedulable_outside_the_night(self):
        write_task(self.root, "sync.md", project="life", status="ready",
                   duty="nightly", created="2026-09-01")
        picked = tasks.launch_order(self.root, PROJECTS, "personal", "sonnet", 3,
                                    done={}, period_keys={"weekly": "2026-08-06"})
        self.assertEqual(self.names(picked), ["queue.md"])

    def test_unsupported_duty_period_is_never_scheduled(self):
        # "daily" is among these: it was dropped for behaving like "nightly".
        for period in ("hourly", "daily", "montly"):
            with self.subTest(period=period):
                write_task(self.root, "recur.md", project="life", status="ready",
                           duty=period, created="2026-09-01")
                self.assertEqual(self.names(self.order()), ["queue.md"])
                self.assertIn((self.path("recur.md"), period, False),
                              tasks.duties(self.root, PROJECTS, "personal"))

    def test_filler_only_once_the_queue_is_exhausted(self):
        write_task(self.root, "tidy.md", project="life", status="ready",
                   priority="high", created="2026-08-01", filler="true")
        self.assertEqual(self.names(self.order(count=1)), ["queue.md"])
        self.assertEqual(self.names(self.order(count=2)), ["queue.md", "tidy.md"])

    def test_filler_beats_parallel_padding(self):
        # Padding only duplicates work already picked, so it goes last.
        write_task(self.root, "queue.md", project="side-projects", status="ready",
                   priority="high", created="2026-08-01", parallel="true")
        write_task(self.root, "tidy.md", project="life", status="ready",
                   created="2026-08-01", filler="true")
        self.assertEqual(self.names(self.order(count=3)),
                         ["queue.md", "tidy.md", "queue.md"])

    def test_classes_are_excluded_from_the_ordinary_queue(self):
        write_task(self.root, "sync.md", project="life", status="ready",
                   priority="high", created="2026-08-01", duty="nightly")
        write_task(self.root, "tidy.md", project="life", status="ready",
                   priority="high", created="2026-08-01", filler="true")
        picked = tasks.pick_multi(self.root, PROJECTS, "personal", "sonnet", 5)
        self.assertEqual(self.names(picked), ["queue.md"])

    def test_classes_are_tagged_for_the_caller(self):
        write_task(self.root, "sync.md", project="life", status="ready",
                   duty="nightly", created="2026-09-01")
        write_task(self.root, "tidy.md", project="life", status="ready",
                   filler="true", created="2026-09-01")
        got = {Path(t["path"]).name: t["sched"] for t in self.order(count=5)}
        self.assertEqual(got, {"sync.md": "duty", "queue.md": None,
                               "tidy.md": "filler"})

    def test_duty_still_obeys_prerequisites_and_model_floor(self):
        write_task(self.root, "sync.md", project="life", status="ready",
                   duty="nightly", created="2026-09-01", model="opus")
        self.assertEqual(self.names(self.order()), ["queue.md"])
        write_task(self.root, "sync.md", project="life", status="ready",
                   duty="nightly", created="2026-09-01",
                   prerequisites="missing.md")
        self.assertEqual(self.names(self.order()), ["queue.md"])


if __name__ == "__main__":
    unittest.main()


class TestRepairStuck(unittest.TestCase):
    """Tasks left `in-progress` by a session that died are reset to `ready`."""

    TTL = 4.5 * 3600

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def stale(self, name, body, age_s):
        p = self.root / "tasks" / name
        p.write_text(body)
        os.utime(p, (time.time() - age_s, time.time() - age_s))
        return p

    def repair(self):
        return tasks.repair_stuck(self.root, time.time(), self.TTL, "2026-09-08")

    def test_old_in_progress_task_becomes_ready_again(self):
        p = self.stale("t.md", "---\ntitle: X\nstatus: in-progress\n---\n\n"
                               "## Notes\n\n- 2026-08-01: started\n", 10 * 3600)
        repaired = self.repair()
        self.assertEqual([n for n, _ in repaired], ["t.md"])
        self.assertIn("status: ready", p.read_text())
        self.assertNotIn("status: in-progress", p.read_text())
        # The last note the dead session left must survive: the repaired task
        # is resumed from it, not restarted.
        self.assertIn("- 2026-08-01: started", p.read_text())
        self.assertIn("auto-repaired by the gatekeeper", p.read_text())

    def test_recent_in_progress_task_is_left_alone(self):
        p = self.stale("t.md", "---\ntitle: X\nstatus: in-progress\n---\n", 60)
        self.assertEqual(self.repair(), [])
        self.assertIn("status: in-progress", p.read_text())

    def test_other_statuses_are_never_touched(self):
        for status in ("ready", "done", "blocked", "inbox"):
            p = self.stale(f"{status}.md",
                           f"---\ntitle: X\nstatus: {status}\n---\n", 10 * 3600)
            self.assertEqual(self.repair(), [])
            self.assertIn(f"status: {status}", p.read_text())

    def test_a_task_without_a_notes_section_gets_one(self):
        p = self.stale("t.md", "---\ntitle: X\nstatus: in-progress\n---\n\n"
                               "## Context\n\nsomething\n", 10 * 3600)
        self.assertEqual([n for n, _ in self.repair()], ["t.md"])
        text = p.read_text()
        self.assertIn("## Notes", text)
        self.assertIn("## Context", text)

    def test_a_status_line_in_the_body_is_not_rewritten(self):
        p = self.stale("t.md", "---\ntitle: X\nstatus: in-progress\n---\n\n"
                               "## Context\n\nRun `gate.py status: in-progress`\n",
                       10 * 3600)
        self.repair()
        self.assertIn("Run `gate.py status: in-progress`", p.read_text())

    def test_repair_is_idempotent(self):
        self.stale("t.md", "---\ntitle: X\nstatus: in-progress\n---\n", 10 * 3600)
        self.assertEqual(len(self.repair()), 1)
        self.assertEqual(self.repair(), [])
