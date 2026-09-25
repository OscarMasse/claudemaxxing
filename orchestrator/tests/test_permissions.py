import fnmatch
import subprocess
import sys
import unittest
from pathlib import Path

from lib import permissions

ORCH = Path(__file__).resolve().parent.parent


def denied(rules, command):
    """Approximates the harness: a Bash(...) rule is a glob over the command."""
    for rule in rules:
        if rule.startswith("Bash(") and fnmatch.fnmatchcase(command, rule[5:-1]):
            return True
    return False


PUSHES = [
    "git push https://github.com/o/r.git agent/x",
    "rtk git push https://github.com/o/r.git agent/x",
    "git -C /tmp/wt push origin agent/x",
]
GH_WRITES = [
    "gh pr comment 12 --body hi",
    "rtk gh pr comment 12 --body hi",
    "gh -R o/r pr comment 12 --body hi",
    "gh pr review 12 --approve",
    "gh pr merge 12",
    "gh pr edit 12 --title t",
    "gh pr close 12",
    "gh issue comment 3 --body hi",
    "gh api repos/o/r/issues/1/comments -X POST",
    "gh api -X PATCH repos/o/r/pulls/1",
    "gh api --method=DELETE repos/o/r/git/refs/heads/x",
    "rtk gh api repos/o/r/issues/1/comments -f body=hi",
]
GH_READS = [
    "gh pr list",
    "gh pr view 12 --comments",
    "gh pr diff 12",
    "gh pr checks 12",
    "rtk gh pr view 12",
    "gh api repos/o/r/pulls/1/comments",
    "gh api -X GET repos/o/r/pulls",
]
IRREVERSIBLE = [
    "git push --force origin agent/x",
    "git push origin agent/x --force",
    "git push -f origin agent/x",
    "git push origin agent/x -f",
    "git push --force-with-lease origin agent/x",
    "rtk git push origin agent/x --force-with-lease",
    "git push origin +agent/x",
    "git -C /tmp/wt push --force origin agent/x",
    "git filter-branch --tree-filter x HEAD",
    "cat ~/.config/backlog-agents/github-token",
    "cat /Users/o/.ssh/id_ed25519",
]
ALWAYS_ALLOWED = [
    "git reset --hard HEAD~1",
    "git push --follow-tags https://github.com/o/r.git agent/x",
    "git status",
]


class TestDenyRules(unittest.TestCase):
    def test_non_pr_deliveries_deny_pushes_and_gh_writes(self):
        for delivery in ("local", "branch", ""):
            rules = permissions.deny_rules(delivery)
            for cmd in PUSHES + GH_WRITES + IRREVERSIBLE:
                self.assertTrue(denied(rules, cmd), f"{delivery!r}: {cmd}")
            for cmd in GH_READS + ["git reset --hard HEAD~1", "git status"]:
                self.assertFalse(denied(rules, cmd), f"{delivery!r}: {cmd}")

    def test_pr_keeps_push_and_gh_but_not_force(self):
        rules = permissions.deny_rules("pr")
        for cmd in PUSHES + GH_WRITES + GH_READS + ALWAYS_ALLOWED:
            self.assertFalse(denied(rules, cmd), cmd)
        for cmd in IRREVERSIBLE:
            self.assertTrue(denied(rules, cmd), cmd)

    def test_credential_files_denied_to_read_tool(self):
        for delivery in ("pr", "local"):
            rules = permissions.deny_rules(delivery)
            self.assertIn("Read(~/.config/backlog-agents/github-token)", rules)
            self.assertIn("Read(~/.ssh/**)", rules)

    def test_cli_prints_one_rule_per_line(self):
        out = subprocess.run([sys.executable, "lib/permissions.py", "local"],
                             cwd=ORCH, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.splitlines(), permissions.deny_rules("local"))
        self.assertIn("Bash(rtk git push*)", out.stdout.splitlines())
        self.assertIn("Bash(gh pr comment*)", out.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
