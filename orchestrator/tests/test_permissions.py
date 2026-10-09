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
    "gh stack submit --auto", "gh stack link 41 agent/x", "gh stack sync",
    "gh pr comment 12 --body hi",
    "rtk gh pr comment 12 --body hi",
    "gh -R o/r pr comment 12 --body hi",
    "gh pr review 12 --approve",
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
    "git push origin +agent/x",
    "git push --force-with-lease origin +agent/x",
    "git push --mirror https://github.com/o/r.git",
    "git push --force-with-lease origin agent/x --force",
    "git -C /tmp/wt push --force origin agent/x",
    "git filter-branch --tree-filter x HEAD",
    "cat ~/.config/backlog-agents/github-token",
    "cat /Users/o/.ssh/id_ed25519",
]
# A `pr` session may refresh its own agent/* PR branch, always with a lease.
LEASE_ALLOWED = [
    "git push --force-with-lease origin agent/x",
    "git push --force-with-lease https://github.com/o/r.git agent/x",
    "git push --force-with-lease origin HEAD:agent/x",
    "rtk git push --force-with-lease origin agent/x",
    "git -C /tmp/wt push --force-with-lease origin agent/x",
    "rtk git -C /tmp/wt push --force-with-lease origin agent/x",
    "git push --force-with-lease=agent/x:abc123 origin agent/x",
    "git push --force-with-lease=agent/x:abc123 https://github.com/o/r.git agent/x",
    "git push --force-with-lease --force-if-includes origin agent/x",
    "git push --force-with-lease origin agent/claudemaxxing-fix-main",
    "git push --force-with origin agent/x",
    "git push --force-with-lease origin agent/fix-origin",
    "git push --force-with-lease origin agent/claudemaxxing-prune-logs",
    "git push --force-with-lease origin agent/x 2>&1",
    "git push -u --force-with-lease --no-verify origin agent/x > /tmp/log",
]
# Lease pushes still denied: no explicit refspec (git would follow
# push.default), towards main/master, or with the flag after the refspec.
LEASE_DENIED = [
    "git push --force-with-lease",
    "rtk git push --force-with-lease",
    "git -C /tmp/wt push --force-with-lease",
    "git push --force-with-lease origin",
    "git push --force-with-lease https://github.com/o/r.git",
    "git push --force-with-lease=agent/x:abc123 origin",
    "git push --force-with-lease origin HEAD",
    "git push --force-with-lease origin main",
    "rtk git push --force-with-lease origin main",
    "git -C /tmp/wt push --force-with-lease origin main",
    "git push --force-with-lease origin main --force-if-includes",
    "git push --force-with-lease origin HEAD:main",
    "git push --force-with-lease origin agent/x:main",
    "git push --force-with-lease origin HEAD:refs/heads/main",
    "git push --force-with-lease=main:abc123 origin agent/x",
    "git push --force-with-lease=main origin main",
    "git push --force-with-lease https://github.com/o/r.git master",
    "rtk git push origin agent/x --force-with-lease",
    "git push --force-with-lease origin @",
    "git push --force-with-lease origin --all",
    "git push --force-with-lease origin --branches",
    "git push --force-with-lease origin --tags",
    "git push --force-with-lease --prune origin agent/x",
    "git push --force-with-lease --repo=origin",
    "git push --force-with-lease origin 'main'",
    'git push --force-with-lease origin "main"',
    "git push --force-with-lease origin 'HEAD:main'",
    "git push --force-with origin main",
    "git push --force-w origin main",
    "git push --force-with-leas origin HEAD:main",
    "git push --force-with origin",
    "git push --force-with",
    "git push --force-with=main:abc123 origin agent/x",
    "git push --force-with-lease origin heads/main",
    "git push --force-with-lease origin agent/x:heads/main",
    "git push --force-with-lease origin agent/x:'main'",
    'git push --force-with-lease origin agent/x:"master"',
    "git push --force-with-lease --force-if-includes",
    "rtk git push --force-with-lease --force-if-includes",
    "git push --force-with-lease -u",
    "git push --force-with-lease --set-upstream",
    "git push --force-with-lease --no-verify",
    "git push --force-with-lease 2>&1",
    "git push --force-with-lease origin 2>&1",
    "git push --force-with-lease > /tmp/log",
    "git push --force-with-lease origin --",
]
MERGES = [
    "gh pr -R o/r merge 5", "gh pr --repo o/r merge 5", "rtk gh pr --repo=o/r merge 5",
    "gh -Ro/r pr merge 5",
    "rtk gh -R o/r pr merge 12 --auto",
    "gh pr merge 12", "gh pr merge 12 --auto --squash", "gh pr merge --rebase 12",
    "rtk gh pr merge 12", "gh -R o/r pr merge 12", "gh --repo o/r pr merge 12",
    "gh pr merge 12 -R o/r", "gh stack merge",
    "gh api -X PUT repos/o/r/pulls/12/merge",
    "rtk gh api --method=PUT repos/o/r/pulls/12/merge -f merge_method=squash",
    "gh api graphql -f query='mutation{enablePullRequestAutoMerge(input:{})}'",
    "gh api graphql -f query='mutation{mergePullRequest(input:{})}'",
]
PR_ALLOWED = [
    "gh pr create --draft --title t",
    "gh pr create --title 'Fix merge conflict handling' --body 'merge'",
    "gh pr view 12 --json mergeable,mergeStateStatus", "gh pr checks 12",
    "gh pr ready 12", "gh pr ready --undo 12",
    "git push https://github.com/o/r.git agent/x",
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

    def test_pr_may_lease_push_agent_branches_only(self):
        for employer in (False, True):
            rules = permissions.deny_rules("pr", employer=employer)
            for cmd in LEASE_ALLOWED:
                self.assertFalse(denied(rules, cmd), cmd)
            for cmd in LEASE_DENIED + IRREVERSIBLE:
                self.assertTrue(denied(rules, cmd), cmd)

    def test_non_pr_deliveries_deny_every_lease_push(self):
        for delivery in ("local", "branch", ""):
            rules = permissions.deny_rules(delivery)
            for cmd in LEASE_ALLOWED + LEASE_DENIED:
                self.assertTrue(denied(rules, cmd), f"{delivery!r}: {cmd}")

    def test_employer_pr_allows_push_and_own_pr_only(self):
        rules = permissions.deny_rules("pr", employer=True)
        for cmd in PUSHES + ["gh pr create --draft", "gh pr edit 3", "gh pr ready 3"] + GH_READS:
            self.assertFalse(denied(rules, cmd), cmd)
        for cmd in ["gh pr comment 3", "gh pr review 3", "gh repo delete x",
                    "gh api -X DELETE repos/a/b", "gh issue create"] + MERGES + IRREVERSIBLE:
            self.assertTrue(denied(rules, cmd), cmd)

    def test_every_delivery_denies_merges(self):
        for delivery in ("pr", "branch", "local", ""):
            rules = permissions.deny_rules(delivery)
            for cmd in MERGES:
                self.assertTrue(denied(rules, cmd), f"{delivery!r}: {cmd}")
        rules = permissions.deny_rules("pr")
        for cmd in PR_ALLOWED:
            self.assertFalse(denied(rules, cmd), cmd)

    def test_credential_files_denied_to_read_tool(self):
        for delivery in ("pr", "local"):
            rules = permissions.deny_rules(delivery)
            self.assertIn("Read(~/.config/backlog-agents/github-token)", rules)
            self.assertIn("Read(~/.ssh/**)", rules)

    def test_readonly_dir_denies_writes_but_not_reads(self):
        ro = "/Users/o/projects/big-assets"
        rules = permissions.deny_rules("pr", [ro])
        for tool in ("Edit", "Write", "NotebookEdit"):
            self.assertIn(f"{tool}({ro}/**)", rules)
        for cmd in (f"git -C {ro} commit -m x", f"rtk git -C {ro} checkout -b y",
                    f"git -C {ro} reset --hard", f"echo x > {ro}/a.txt",
                    f"cat a >> {ro}/b", f"rm -rf {ro}/sprites",
                    f"cp a.png {ro}/a.png", f"sed -i s/a/b/ {ro}/f"):
            self.assertTrue(denied(rules, cmd), cmd)
        for cmd in (f"git -C {ro} log --oneline", f"git -C {ro} status",
                    f"cat {ro}/a.txt", f"ls {ro}"):
            self.assertFalse(denied(rules, cmd), cmd)
        self.assertNotIn(f"Edit({ro}/**)", permissions.deny_rules("pr"))

    def test_cli_takes_readonly_dirs_after_the_delivery(self):
        out = subprocess.run([sys.executable, "lib/permissions.py", "local", "/r/o"],
                             cwd=ORCH, capture_output=True, text=True, check=True)
        self.assertIn("Write(/r/o/**)", out.stdout.splitlines())

    def test_cli_prints_one_rule_per_line(self):
        out = subprocess.run([sys.executable, "lib/permissions.py", "local"],
                             cwd=ORCH, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.splitlines(), permissions.deny_rules("local"))
        self.assertIn("Bash(rtk git push*)", out.stdout.splitlines())
        self.assertIn("Bash(gh pr comment*)", out.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
