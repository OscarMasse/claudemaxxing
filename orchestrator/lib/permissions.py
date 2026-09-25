"""Per-delivery --disallowedTools rules for headless sessions.

Sessions run in bypassPermissions, so these deny rules are what actually
enforces the delivery rails: a deny is evaluated by the harness before the
model's judgement gets a say, costs no tokens and needs no hook script.

A Bash rule is a glob over the command string (Claude Code splits compound
commands on shell operators and checks each part). It stops a session that
forgot the prose, not one that spells the command differently, so every family
enumerates its known spellings:
- the `rtk` prefix, because the token-saving hook rewrites `git ...` and
  `gh ...` into `rtk git ...` and `rtk gh ...`;
- global options placed before the subcommand (`git -C dir push`,
  `gh -R owner/repo pr comment`);
- flags placed after the positional arguments (`git push origin b --force`).

Usage: python3 lib/permissions.py <delivery>   (one rule per line; an empty
delivery means the deliveryless digest/auto modes)
"""
import sys

# Prefixes a git / gh invocation can take before its subcommand.
GIT_PREFIXES = ("git ", "git -C * ", "git -c * ")
GH_PREFIXES = ("gh ", "gh -R * ", "gh --repo * ", "gh --repo=* ")

# Mutating gh subcommands. Reads (`gh pr list/view/diff/checks`, `gh issue
# view`) stay allowed: job and local tasks rely on them to follow reviews.
GH_MUTATING = (
    "pr comment", "pr review", "pr merge", "pr edit", "pr close",
    "pr create", "pr ready", "pr reopen",
    "issue comment", "issue create", "issue edit", "issue close",
    "issue reopen", "issue delete",
    "release create", "release edit", "release delete",
    "repo create", "repo edit", "repo delete", "gist create",
    "workflow run", "label create",
)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
METHOD_FLAGS = ("-X ", "-X", "--method ", "--method=")
# gh api switches to POST by itself as soon as a field or a body is passed, so
# these flags are writes even without an explicit method. This also blocks
# `gh api graphql -f query=...` reads: accepted, `gh pr view --json` covers them.
BODY_FLAGS = ("-f ", "-F ", "--field", "--raw-field", "--input")

# Credential files. The token is exported to `pr` sessions as GH_TOKEN, so a
# session could still print it from its environment: these denies keep the
# files themselves (and the SSH keys) out of reach, not the variable.
CREDENTIAL_PATHS = ("~/.config/backlog-agents/github-token", "~/.ssh/**")
CREDENTIAL_BASH = (".config/backlog-agents/github-token", ".ssh/")


def _with_rtk(commands):
    return [p + c for c in commands for p in ("", "rtk ")]


def push_rules():
    """Any push. Denied to every session that is not `pr`."""
    return _with_rtk([f"{g}push*" for g in GIT_PREFIXES])


def gh_write_rules():
    """Mutating GitHub calls. Denied to every session that is not `pr`:
    a comment posted by a local task lands on a real repo under the owner's name."""
    cmds = [f"{g}{sub}*" for g in GH_PREFIXES for sub in GH_MUTATING]
    for flag in METHOD_FLAGS:
        for method in WRITE_METHODS:
            for spelled in (method, method.lower()):
                cmds.append(f"gh api*{flag}{spelled}*")
    cmds += [f"gh api* {flag}*" for flag in BODY_FLAGS]
    return _with_rtk(cmds)


def irreversible_rules():
    """History rewriting on a remote and filter-branch. Denied to every session,
    `pr` included: a pushed branch is the owner's review surface.

    Force flags are matched anywhere after `push`. A `+refspec`
    (`git push origin +branch`) is a force push too and is caught by the
    `push * +*` form. Accepted gaps: bundled short flags (`-uf`) and a
    `remote.<name>.push` refspec with a `+` set in git config, and env-var
    prefixes (`FOO=1 git push`) if the harness does not strip them. The
    `git -C * ` prefix over-matches (`git -C wt log --grep push` is denied):
    accepted, a false refusal costs less than a missed push.
    `git reset --hard` is deliberately NOT here: inside a disposable worktree
    it is the routine way to abandon a bad attempt, and nothing is lost.
    """
    cmds = []
    for g in GIT_PREFIXES:
        cmds += [f"{g}push*--force*", f"{g}push* -f*", f"{g}push * +*",
                 f"{g}filter-branch*"]
    return _with_rtk(cmds)


def credential_rules():
    """Credential reads, every session: the agent token and SSH keys."""
    rules = [f"Read({p})" for p in CREDENTIAL_PATHS]
    rules += [f"Bash(*{p}*)" for p in CREDENTIAL_BASH]
    return rules


def deny_rules(delivery):
    rules = []
    if delivery != "pr":
        rules += push_rules() + gh_write_rules()
    rules += irreversible_rules()
    bash = [f"Bash({c})" for c in rules]
    return bash + credential_rules()


if __name__ == "__main__":
    print("\n".join(deny_rules(sys.argv[1] if len(sys.argv) > 1 else "")))
