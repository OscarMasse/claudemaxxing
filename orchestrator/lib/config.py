"""Load the orchestrator's config.toml with the standard library (tomllib).

The syntax is plain TOML; the structure is pinned by `validate`:

  key = value           # top-level scalar: a shared default for every account
  [[accounts]]          # an array of flat tables, one per subscription
  name = "max"
  [[projects]]          # an array of flat tables, one per project
  name = "work"
  dirs = ["~/work"]     # dirs and optional_dirs are arrays of strings

Every other value is a scalar (string, integer, float, boolean). Times and
dates are quoted strings ("05:59", "2026-08-19"), parsed downstream. A nested
table, an array where a scalar is expected, or a missing [[accounts]] or
[[projects]] is refused at load time. Until 2026-10-01 the file was a
hand-rolled YAML subset (config.yaml); scripts/config_yaml_to_toml.py
migrates one, and the engine refuses to start on a config.yaml alone.

Sections used: `accounts` and `projects` (see config.toml for the full story).

Accessors:
  misconfigured_account(acct) -> a problem string, or None. The budget unit is
                    USD since 2026-09-12; an account still carrying a retired
                    `*_tokens` key, or missing its USD caps, is refused rather
                    than converted (default when a key is absent, never when it
                    is present and unreadable).
  accounts(cfg)  -> list of merged account dicts. Each account inherits every
                    top-level scalar as a default and overrides it with its own
                    keys, so shared knobs (regimes, slices, rates) are written
                    once while calibration lives per account.
  projects(cfg)  -> {name: {name, account, dirs (expanded list), rank,
                    expedite, local_only_default}}. `rank` breaks ties
                    between equally urgent tasks and `class: expedite` puts a
                    project's tasks ahead of the whole queue, see
                    lib/tasks.py. `local_only_default` is a floor on the
                    tasks' `delivery:` key, see lib/tasks.py. A project entry
                    carrying any other key (the retired `priority` included)
                    is refused.

Resolution (the single place that decides which config file is live):
  1. `ORCH_CONFIG` env var, when set (explicit override, tests and one-offs).
  2. `<backlog root>/config.toml`, when it exists (the operator's real config,
     living outside the public repo so it can never be committed here). A
     config.yaml there without a config.toml fails loud with the migration
     command (ConfigFormatError).
  3. The repo's own `orchestrator/config.toml` (the documented example), ONLY
     when ORCH_EXAMPLE=1 asks for it (tests, e2e sandbox).
The backlog root itself is, in order: the ORCH_ROOT env var, the BACKLOG_ROOT
env var, the path recorded by install.sh in ROOT_FILE, and the repo root only
under ORCH_EXAMPLE=1. With none of them, every entry point fails loudly
(BacklogRootError) instead of silently reading the example data: on
2026-09-26 and 2026-09-27 that silent fallback printed fake accounts as real
alarms and made a manual launch drop every task as "no such task".

CLI (used by run.sh so shell scripts never parse the config themselves):
  python3 lib/config.py resolve            # print the resolved config path
  python3 lib/config.py backlog-root       # print the resolved backlog root
  python3 lib/config.py record-root <path> # write <path> to ROOT_FILE (install)
  python3 lib/config.py <config.toml> get <key> [default]
  python3 lib/config.py <config.toml> accounts
  python3 lib/config.py <config.toml> first-account
  python3 lib/config.py <config.toml> account <name> <key> [default]
  python3 lib/config.py <config.toml> account-dirs <name>
  python3 lib/config.py <config.toml> account-projects <name>
  python3 lib/config.py <config.toml> project-dirs <name>
  python3 lib/config.py <config.toml> project-optional-dirs <name>
  python3 lib/config.py <config.toml> account-optional-dirs <name>
  python3 lib/config.py <config.toml> project-account <name>
  python3 lib/config.py <config.toml> project-workdir <name>  # worktree|main
  python3 lib/config.py <config.toml> digest-file      # today/tomorrow's digest
                                                       # path per digest_time
                                                       # (ORCH_NOW overrides
                                                       # "now", for tests)
"""
import os
import sys

# Every entry point imports this module, so the interpreter floor lives here,
# before the first import a 3.9 system python cannot satisfy.
if sys.version_info < (3, 14):
    sys.exit(f"claudemaxxing needs Python 3.14+, but {sys.executable} is "
             f"{sys.version.split()[0]}. Put a 3.14 interpreter first on PATH "
             f"(Homebrew: /opt/homebrew/bin) or re-run orchestrator/install.sh.")

import tomllib  # noqa: E402
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import digest as digest_lib  # noqa: E402


def repo_root():
    """The checkout that holds this code: lib/ -> orchestrator/ -> repo."""
    return Path(__file__).resolve().parents[2]


class BacklogRootError(RuntimeError):
    """Neither the env nor the recorded file says where the backlog lives."""


def root_file():
    """The user-level file install.sh records the backlog root in, so an
    interactive shell resolves the same root as the scheduled jobs."""
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "claudemaxxing" / "backlog-root"


def example_requested():
    """ORCH_EXAMPLE=1: the only way to get the repo's example data."""
    return os.environ.get("ORCH_EXAMPLE") == "1"


def backlog_root():
    """Where tasks/, digests/, NEEDS-HUMAN.md and orchestrator/state/ live:
    ORCH_ROOT, then BACKLOG_ROOT, then root_file(), then the repo root under
    ORCH_EXAMPLE=1. Raises BacklogRootError when none applies."""
    for var in ("ORCH_ROOT", "BACKLOG_ROOT"):
        env = os.environ.get(var)
        if env:
            return Path(env).expanduser()
    recorded = root_file()
    if recorded.is_file():
        text = recorded.read_text().strip()
        if text:
            return Path(text).expanduser()
    if example_requested():
        return repo_root()
    raise BacklogRootError(
        f"backlog root unknown: set BACKLOG_ROOT, or run install.sh to record "
        f"it in {recorded} (ORCH_EXAMPLE=1 uses the repo's example data)")


CONFIG_NAME = "config.toml"
RETIRED_NAMES = ("config.yaml", "config.yml")


class ConfigFormatError(BacklogRootError):
    """A retired config.yaml sits where config.toml is expected."""


def migration_hint(directory):
    """The exact command that turns `directory`'s config.yaml into TOML."""
    script = repo_root() / "scripts" / "config_yaml_to_toml.py"
    return (f"{directory} has a retired config.yaml and no {CONFIG_NAME}: "
            f"the format is TOML since 2026-10-01, migrate it with "
            f"`python3 {script} {directory}`")


def _find(directory):
    """`directory`/config.toml when it exists, else None. A retired
    config.yaml (or .yml) without it fails loud with the migration command
    rather than falling through to another config."""
    candidate = Path(directory) / CONFIG_NAME
    if candidate.exists():
        return candidate
    if any((Path(directory) / n).exists() for n in RETIRED_NAMES):
        raise ConfigFormatError(migration_hint(directory))
    return None


def resolve_path(root=None):
    """The live config file, in resolution order: ORCH_CONFIG env override,
    then <backlog root>/config.toml when it exists, then the repo's example
    orchestrator/config.toml under ORCH_EXAMPLE=1 only. `root` overrides
    backlog_root(). Raises BacklogRootError when the root has no config and
    the example was not asked for, ConfigFormatError when the root still
    holds a config.yaml."""
    explicit = os.environ.get("ORCH_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    root = Path(root) if root is not None else backlog_root()
    found = _find(root)
    if found is not None:
        return found
    if not example_requested():
        raise BacklogRootError(
            f"no {CONFIG_NAME} in backlog root {root} (set BACKLOG_ROOT to the "
            f"real backlog, or ORCH_EXAMPLE=1 for the repo's example config)")
    return repo_root() / "orchestrator" / CONFIG_NAME


# The only shapes a config may take. tomllib parses the syntax; these checks
# pin the structure, so a typo or a misplaced table fails loud at load time
# instead of silently changing a schedule.
SECTIONS = ("accounts", "projects")
LIST_KEYS = ("dirs", "optional_dirs")
_SCALARS = (bool, int, float, str)


def _check_scalar(where, key, value):
    if isinstance(value, dict):
        raise ValueError(f"config: {where} key {key!r} is a table, expected a "
                         f"scalar (string, number or boolean)")
    if isinstance(value, list):
        raise ValueError(f"config: {where} key {key!r} is an array, expected a "
                         f"scalar (string, number or boolean)")
    if not isinstance(value, _SCALARS):
        # TOML dates and times: the engine parses them downstream from
        # strings, so they must be quoted ("05:59", not 05:59:00).
        raise ValueError(f"config: {where} key {key!r} has type "
                         f"{type(value).__name__}, write it as a quoted string")


def _check_entry(section, entry):
    where = f"[[{section}]] entry {entry.get('name', '?')!r}"
    for key, value in entry.items():
        if key in LIST_KEYS and section == "projects":
            if not (isinstance(value, list)
                    and all(isinstance(v, str) for v in value)):
                raise ValueError(f"config: {where} key {key!r} must be an "
                                 f"array of strings")
            continue
        _check_scalar(where, key, value)


def validate(cfg):
    """Refuse any structure the engine does not read: top-level keys are
    scalars except the two arrays of tables, whose entries are flat (scalars,
    plus the string arrays in LIST_KEYS). Returns cfg."""
    for key, value in cfg.items():
        if key in SECTIONS:
            if not (isinstance(value, list)
                    and all(isinstance(e, dict) for e in value)):
                raise ValueError(f"config: {key!r} must be an array of tables "
                                 f"([[{key}]])")
            for entry in value:
                _check_entry(key, entry)
        else:
            _check_scalar("top-level", key, value)
    for key in SECTIONS:
        if not cfg.get(key):
            raise ValueError(f"config: no [[{key}]] table")
    return cfg


def load(path):
    path = Path(path)
    if path.suffix in (".yaml", ".yml"):
        raise ConfigFormatError(migration_hint(path.parent))
    with path.open("rb") as f:
        try:
            cfg = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"config: {path}: {e}") from None
    return validate(cfg)


# Retired keys, each with where its job went. A config that still carries
# one is refused rather than silently ignored: the number in it was the
# owner's calibration, and pretending to honour it would be worse than
# saying it no longer means anything.
# - The token-era budget keys (specs/2026-09-12-usd-budget.md): no factor
#   converts a model-blind token count into dollars.
# - The hand-entered USD caps and the promo multiplier on them: the caps are
#   derived from the account's own rate-limit readings since 2026-09-26
#   (lib/ratelimits.py), and a reading already includes any promotion.
_TOKEN_NOTE = "the unit is USD since 2026-09-12 (see specs)"
_CAPS_NOTE = "caps are derived from rate-limit readings (lib/ratelimits.py)"
RETIRED_KEYS = {
    "weekly_cap_tokens": _TOKEN_NOTE, "window_cap_tokens": _TOKEN_NOTE,
    "p90_daily_tokens": _TOKEN_NOTE, "est_session_tokens": _TOKEN_NOTE,
    "fable_min_surplus_tokens": _TOKEN_NOTE, "opus_min_surplus_tokens": _TOKEN_NOTE,
    "weekly_cap_usd": _CAPS_NOTE, "window_cap_usd": _CAPS_NOTE,
    "p90_daily_usd": _CAPS_NOTE, "promo_multiplier": _CAPS_NOTE,
    "promo_until": _CAPS_NOTE,
}


def misconfigured_account(acct):
    """Why a merged account dict cannot be scheduled, or None when it can."""
    for key, note in RETIRED_KEYS.items():
        if key in acct:
            return f"retired key {key}, {note}"
    return None


def scalars(cfg):
    """The top-level scalar keys (everything that is not a section)."""
    return {k: v for k, v in cfg.items() if not isinstance(v, list)}


def accounts(cfg):
    """Merged account dicts: top-level scalars as defaults, account keys win."""
    base = scalars(cfg)
    out, seen = [], set()
    for a in cfg.get("accounts") or []:
        if "name" not in a:
            raise ValueError("config: account entry without a name")
        if a["name"] in seen:
            raise ValueError(f"config: duplicate account name {a['name']!r}")
        seen.add(a["name"])
        merged = dict(base)
        merged.update(a)
        merged.setdefault("claude_config_dir", "~/.claude")
        merged["claude_config_dir"] = os.path.expanduser(str(merged["claude_config_dir"]))
        merged.setdefault("claude_bin", "claude")
        merged.setdefault("claude_model", "sonnet")
        merged.setdefault("claude_effort", "low")
        out.append(merged)
    return out


# Every key a project entry may carry. Anything else is refused rather than
# ignored: a misspelled `class: expedit` or a leftover `priority: 5` would
# otherwise change the launch order silently.
PROJECT_KEYS = ("name", "account", "dirs", "optional_dirs", "rank", "class",
                "local_only_default", "workdir")
# `optional_dirs` are repos a task of the project only sometimes needs (webapp's
# ~1 GB big-assets checkout): a task names the ones it writes to with
# `uses: [<basename>, ...]` and gets a worktree of each (lib/workspace.py);
# every other optional dir is handed to the session read-only, as its main
# checkout under deny rules (lib/permissions.py), so no task pays for a
# worktree it never uses.
# `workdir: main` exempts a whole project from per-task worktrees
# (lib/workspace.py): for a repo that is a notes store, not code (~/notes),
# where every task commits in place. Absent = a worktree per task.
PROJECT_WORKDIRS = ("worktree", "main")
PROJECT_CLASSES = ("standard", "expedite")
# The project `priority` was a hard first sort key: every task of a
# better-ranked project, even a `low` one, launched before any task of the
# others. `rank` only breaks ties between equally urgent tasks, so the old
# numbers do not carry over and are refused, not reinterpreted.
RETIRED_PROJECT_KEYS = {
    "priority": "use `rank` (a tie-breaker, see lib/tasks.py) and "
                "`class: expedite` for work that must go first",
}


def projects(cfg):
    """Project registry {name: project}. Validates account references and
    refuses unknown or retired keys."""
    known = {a["name"] for a in accounts(cfg)}
    out = {}
    for p in cfg.get("projects") or []:
        if "name" not in p:
            raise ValueError("config: project entry without a name")
        if "account" not in p:
            raise ValueError(f"config: project {p['name']!r} without an account")
        if p["account"] not in known:
            raise ValueError(f"config: project {p['name']!r} references "
                             f"unknown account {p['account']!r}")
        if p["name"] in out:
            raise ValueError(f"config: duplicate project name {p['name']!r}")
        for key in p:
            if key in RETIRED_PROJECT_KEYS:
                raise ValueError(f"config: project {p['name']!r} carries retired "
                                 f"key {key!r}: {RETIRED_PROJECT_KEYS[key]}")
            if key not in PROJECT_KEYS:
                raise ValueError(f"config: project {p['name']!r} has unknown "
                                 f"key {key!r}")
        cls = str(p.get("class", "standard"))
        if cls not in PROJECT_CLASSES:
            raise ValueError(f"config: project {p['name']!r} has unknown class "
                             f"{cls!r} (expected one of {', '.join(PROJECT_CLASSES)})")
        workdir = str(p.get("workdir", "worktree"))
        if workdir not in PROJECT_WORKDIRS:
            raise ValueError(f"config: project {p['name']!r} has unknown workdir "
                             f"{workdir!r} (expected one of {', '.join(PROJECT_WORKDIRS)})")
        rank = p.get("rank", 100)
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise ValueError(f"config: project {p['name']!r} rank {rank!r} "
                             f"is not an integer")
        out[p["name"]] = {
            "name": p["name"],
            "account": p["account"],
            "dirs": [os.path.expanduser(d) for d in p.get("dirs", [])],
            "optional_dirs": [os.path.expanduser(d) for d in
                              p.get("optional_dirs", [])],
            "rank": rank,
            "expedite": cls == "expedite",
            "local_only_default": bool(p.get("local_only_default", False)),
            "workdir": workdir,
        }
    return out


def _fmt(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _main(argv):
    try:
        return _dispatch(argv)
    except BacklogRootError as e:
        print(f"config: {e}", file=sys.stderr)
        return 2


def _dispatch(argv):
    if argv[1] == "record-root":
        target = root_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(Path(argv[2]).expanduser().resolve()) + "\n")
        print(target)
        return 0
    if argv[1] == "resolve":
        print(resolve_path())
        return 0
    if argv[1] == "backlog-root":
        print(backlog_root())
        return 0
    cfg = load(argv[1])
    cmd = argv[2]
    if cmd == "get":
        default = argv[4] if len(argv) > 4 else ""
        print(_fmt(cfg.get(argv[3], default)))
        return 0
    if cmd == "accounts":
        for a in accounts(cfg):
            print(a["name"])
        return 0
    if cmd == "first-account":
        print(accounts(cfg)[0]["name"])
        return 0
    if cmd == "account":
        name, key = argv[3], argv[4]
        default = argv[5] if len(argv) > 5 else ""
        for a in accounts(cfg):
            if a["name"] == name:
                print(_fmt(a.get(key, default)))
                return 0
        print(f"config: unknown account {name!r}", file=sys.stderr)
        return 2
    if cmd == "account-projects":
        for p in projects(cfg).values():
            if p["account"] == argv[3]:
                print(p["name"])
        return 0
    if cmd in ("account-dirs", "account-optional-dirs"):
        key = "dirs" if cmd == "account-dirs" else "optional_dirs"
        seen = []
        for p in projects(cfg).values():
            if p["account"] == argv[3]:
                for d in p[key]:
                    if d not in seen:
                        seen.append(d)
        if key == "optional_dirs":
            # A dir another project of the account writes to stays writable.
            writable = {d for p in projects(cfg).values()
                        if p["account"] == argv[3] for d in p["dirs"]}
            seen = [d for d in seen if d not in writable]
        print(" ".join(seen))
        return 0
    if cmd in ("project-dirs", "project-optional-dirs"):
        p = projects(cfg).get(argv[3])
        if p is None:
            print(f"config: unknown project {argv[3]!r}", file=sys.stderr)
            return 2
        print(" ".join(p["dirs" if cmd == "project-dirs" else "optional_dirs"]))
        return 0
    if cmd == "project-local-only":
        p = projects(cfg).get(argv[3])
        if p is None:
            print(f"config: unknown project {argv[3]!r}", file=sys.stderr)
            return 2
        print(_fmt(p["local_only_default"]))
        return 0
    if cmd == "project-workdir":
        p = projects(cfg).get(argv[3])
        if p is None:
            print(f"config: unknown project {argv[3]!r}", file=sys.stderr)
            return 2
        print(p["workdir"])
        return 0
    if cmd == "project-account":
        p = projects(cfg).get(argv[3])
        if p is None:
            print(f"config: unknown project {argv[3]!r}", file=sys.stderr)
            return 2
        print(p["account"])
        return 0
    if cmd == "digest-file":
        # Precedence: ORCH_NOW env (tests) > an explicit ISO argument (run.sh
        # pins the digest session to today's midnight, see run.sh) > real now.
        raw = os.environ.get("ORCH_NOW") or (argv[3] if len(argv) > 3 else None)
        now = datetime.fromisoformat(raw) if raw else datetime.now()
        digest_time = str(cfg.get("digest_time", "07:37"))
        print(digest_lib.digest_file(now, digest_time, backlog_root()))
        return 0
    print(f"config: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
