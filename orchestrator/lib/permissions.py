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

Usage: python3 lib/permissions.py <delivery> [--employer] [<readonly_dir>...]
(one rule per line; an empty delivery means the deliveryless digest/auto
modes; each readonly dir gets readonly_rules)
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
    # gh-stack extension: each of these pushes branches or edits PRs.
    "stack submit", "stack link", "stack push", "stack sync", "stack merge",
    "stack unstack",
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


# What a `pr` session of a local-only (employer) project may still do: open and
# update its own draft PR. Everything else stays denied there, because the
# session runs on the owner's broad keyring login (no scoped token on lumapps).
EMPLOYER_PR_ALLOWED = ("pr create", "pr edit", "pr ready")


def gh_write_rules(allow=()):
    """Mutating GitHub calls. Denied to every session that is not `pr`:
    a comment posted by a local task lands on a real repo under the owner's name.
    `allow` lists mutating subcommands to leave out (see EMPLOYER_PR_ALLOWED)."""
    cmds = [f"{g}{sub}*" for g in GH_PREFIXES for sub in GH_MUTATING
            if sub not in allow]
    for flag in METHOD_FLAGS:
        for method in WRITE_METHODS:
            for spelled in (method, method.lower()):
                cmds.append(f"gh api*{flag}{spelled}*")
    cmds += [f"gh api* {flag}*" for flag in BODY_FLAGS]
    return _with_rtk(cmds)


def merge_rules():
    """Merging a PR, every session, `pr` included: Oscar reviews and tests each
    agent PR and merging stays his gesture only. Covers `gh pr merge` in all its
    forms (`--auto`, `--rebase`, `-R`), `gh stack merge`, the REST merge endpoint
    (`.../pulls/<n>/merge`, any method: a read of it is refused too, accepted)
    and the auto-merge / merge GraphQL mutations, whatever the repo settings."""
    cmds = [f"{g}{sub}*" for g in GH_PREFIXES
            for sub in ("pr merge", "stack merge")]
    cmds += [f"gh {g}* merge*" for g in ("pr -R", "pr --repo", "pr --repo=", "-R", "--repo")
             if g.startswith("pr")]
    cmds += ["gh -R?* pr merge*", "gh api*pulls/*/merge*", "gh api*enablePullRequestAutoMerge*",
             "gh api*mergePullRequest*"]
    return _with_rtk(cmds)


# Refs a lease push must never target, spelled as a destination (`origin
# main`, `HEAD:main`, `refs/heads/main`) or as the lease ref itself.
PROTECTED_REFS = ("main", "master")
# Endings of a lease push that names no refspec, so git falls back to
# push.default (or to the current branch, `HEAD`): a remote, `HEAD`, or one of
# the flags most often written after the lease flag.
NO_REFSPEC_ENDINGS = ("", " origin", " upstream", ".git", " HEAD", " @",
                      "--force-if-includes", " -u", "--set-upstream",
                      "--no-verify", " -v", "--verbose", " -q", "--quiet",
                      "--atomic")
# Lease push options that pick the refs themselves, `main` included.
MULTI_REF_FLAGS = ("--all", "--branches", "--tags", "--prune", "--repo")
# Git accepts any unambiguous abbreviation of a long option, and
# `--force-w` is the shortest one of `--force-with-lease`: rules that need the
# flag's exact end enumerate every spelling, the others match the stem.
LEASE = "--force-with-lease"
LEASE_STEM = "--force-w"
LEASE_SPELLINGS = tuple(LEASE[:n] for n in range(len(LEASE_STEM), len(LEASE) + 1))


def irreversible_rules():
    """History rewriting on a remote and filter-branch. Denied to every session.

    One exception: a session may rewrite its own `agent/*` PR branch, always
    with a lease (`git push --force-with-lease <remote> agent/<task>`, or
    `--force-with-lease=agent/<task>:<sha>`), to refresh a PR after main moved.
    Deny globs cannot express negation, so the families below are written so
    that `--force` stops prefix-matching `--force-with-lease`:
    - plain `--force` and `-f` anywhere after `push` (`--force` matched at the
      end of the command or followed by a space), `--mirror` (it forces every
      ref), and a `+refspec` (`git push origin +branch`, the `push * +*` form);
    - a lease push that names no refspec (the lease flag last, or the command
      ending on a remote, on `HEAD`/`@` or on a common flag such as
      `--force-if-includes` or `-u`), since git would then follow
      push.default, and one that lets git pick the refs (`--all`,
      `--branches`, `--tags`, `--prune`, `--repo`);
    - a lease push towards `main` or `master`, as a destination (bare,
      quoted, `heads/main`, `refs/heads/main`) or as the lease ref;
    - every abbreviation git accepts for `--force-with-lease` (`--force-with`),
      in all the lease families above.
    Accepted gaps: a lease push towards another non-`agent/*` ref (`origin
    feature/x`; the globs cannot say "not agent/"), a wildcard refspec
    (`refs/heads/*:refs/heads/*`), a lease push ending on a flag not listed
    in NO_REFSPEC_ENDINGS, a lease ref with no remote nor refspec
    (`--force-with-lease=agent/x:<sha>` alone: it only forces that ref),
    abbreviations of `--mirror` and of the flags that let git pick the refs
    (`--tag`), tabs instead of spaces, a remote spelled other than `origin`,
    `upstream` or a `.git` URL followed by no refspec, bundled short flags
    (`-uf`), a `remote.<name>.push` refspec with a `+` set in git config, and
    env-var prefixes (`FOO=1 git push`) if the harness does not strip them.
    Accepted false refusals: the lease flag, or one of the flags above,
    placed after the refspec (`git push origin agent/x --force-with-lease`:
    put every flag before the remote), a branch named `main...`/`master...`
    after a space or `heads/`, an `agent/*` branch ending in `.git` or holding `--all`-like
    words after a double dash, and the `git -C * ` prefix over-matching
    (`git -C wt log --grep push` is denied): a false refusal costs less than
    a missed push.
    `git reset --hard` is deliberately NOT here: inside a disposable worktree
    it is the routine way to abandon a bad attempt, and nothing is lost.
    """
    cmds = []
    for g in GIT_PREFIXES:
        cmds += [f"{g}push*--force", f"{g}push*--force *", f"{g}push* -f*",
                 f"{g}push*--mirror*", f"{g}push * +*", f"{g}filter-branch*"]
        cmds += [f"{g}push*{spelled}" for spelled in LEASE_SPELLINGS]
        cmds += [f"{g}push*{LEASE_STEM}*{end}" for end in NO_REFSPEC_ENDINGS if end]
        cmds += [f"{g}push*{LEASE_STEM}*{flag}*" for flag in MULTI_REF_FLAGS]
        for ref in PROTECTED_REFS:
            lease = f"{g}push*{LEASE_STEM}*"
            cmds += [f"{lease} {ref}", f"{lease} {ref} *", f"{lease} '{ref}*",
                     f"{lease} \"{ref}*", f"{lease}:{ref}", f"{lease}:{ref} *",
                     f"{lease}:{ref}'*", f"{lease}:{ref}\"*",
                     f"{lease}:'{ref}*", f"{lease}:\"{ref}*",
                     f"{lease}heads/{ref}*"]
            cmds += [f"{g}push*{spelled}={ref}*" for spelled in LEASE_SPELLINGS]
    return _with_rtk(cmds)


def credential_rules():
    """Credential reads, every session: the agent token and SSH keys."""
    rules = [f"Read({p})" for p in CREDENTIAL_PATHS]
    rules += [f"Bash(*{p}*)" for p in CREDENTIAL_BASH]
    return rules


# Mutating git subcommands denied under a read-only dir. Reads (`log`, `show`,
# `diff`, `status`, `grep`, `ls-files`) stay allowed: reading the repo is why
# it is handed to the session at all.
GIT_MUTATING = (
    "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean",
    "commit", "fetch", "gc", "merge", "mv", "pull", "rebase", "reset",
    "restore", "revert", "rm", "stash", "switch", "tag", "worktree",
)
# Shell commands that write to their path arguments.
SHELL_WRITERS = ("rm", "mv", "cp", "tee", "touch", "mkdir", "rmdir", "ln",
                 "sed -i", "chmod", "truncate")


def readonly_rules(path):
    """Writes under an optional dir the task did not declare in `uses:`
    (lib/workspace.py): the session reads its main checkout, where the owner
    works, so it must not change it.

    The file tools are denied outright on `<path>/**`. Bash is a best effort,
    as for every family here: mutating git subcommands through `git -C
    <path>`, redirections into the path, and the usual writing commands with
    the path among their arguments. Accepted gaps: a command that reaches the
    dir without spelling its absolute path (`cd <path> && git commit`, a
    relative or `~` path, a variable), a writer not listed (a script, `python
    -c`, `dd`), and `git -C <path>/sub`. The worktree path and the backlog
    root stay writable, since they do not start with `<path>/`, but a sibling
    whose name extends the path (`<path>-old`) is caught by the redirect and
    writer forms: accepted, a false refusal costs less than a missed write."""
    path = str(path).rstrip("/")
    rules = [f"{tool}({path}/**)" for tool in ("Edit", "Write", "NotebookEdit")]
    cmds = [f"git -C {path} {sub}*" for sub in GIT_MUTATING]
    cmds += [f"*>*{path}*"]
    cmds += [f"{w} *{path}*" for w in SHELL_WRITERS]
    return rules + [f"Bash({c})" for c in _with_rtk(cmds)]


def deny_rules(delivery, readonly_dirs=(), employer=False):
    """`employer` marks a session of a local-only project: its `pr` delivery
    keeps push and `gh pr create/edit/ready` but not the other mutating gh calls."""
    rules = []
    if delivery != "pr":
        rules += push_rules() + gh_write_rules()
    elif employer:
        rules += gh_write_rules(allow=EMPLOYER_PR_ALLOWED)
    rules += merge_rules() + irreversible_rules()
    bash = [f"Bash({c})" for c in rules]
    out = bash + credential_rules()
    for d in readonly_dirs:
        out += readonly_rules(d)
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    employer = "--employer" in args
    args = [a for a in args if a != "--employer"]
    print("\n".join(deny_rules(args[0] if args else "", args[1:], employer)))
