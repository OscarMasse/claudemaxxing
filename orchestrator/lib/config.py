"""Parse the orchestrator's config.yaml (a small hand-rolled YAML subset, zero deps).

Exactly two shapes are supported, and nothing else:

  key: value            # top-level scalar (the legacy flat format)
  section:              # a top-level key with no value opens a list of flat mappings
    - key: value
      key: value

Scalar coercion: true/false, integers, floats; everything else stays a string,
so times (05:59) and dates (2026-08-19) survive as strings and are parsed
downstream. Comments (#) and blank lines are ignored. No deeper nesting, no
quoting, no multi-line values.

Multi-value fields (like a project's `dirs`) are written in YAML flow style,
`dirs: [~/one, ~/two]`; the legacy space-separated form still parses. See
`split_values`.

Sections used: `accounts` and `projects` (see config.yaml for the full story).

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

Backward compatibility: when the sections are absent, accounts() synthesizes a
single account named "default" (profile ~/.claude, calibration from the flat
keys) and projects() a single project named "default" whose dirs come from the
legacy flat `extra_dirs` key. Tasks without a `project:` frontmatter key route
to the project named "default" when one exists, so a legacy flat config and
legacy task files keep working unchanged.

Resolution (the single place that decides which config file is live):
  1. `ORCH_CONFIG` env var, when set (explicit override, tests and one-offs).
  2. `<backlog root>/config.yaml`, when it exists (the operator's real config,
     living outside the public repo so it can never be committed here).
  3. The repo's own `orchestrator/config.yaml` (the documented example), ONLY
     when ORCH_EXAMPLE=1 asks for it (tests, e2e sandbox).
`.yaml` is the preferred spelling and wins when both exist; `.yml` is still
accepted at every step so an existing install keeps working without a rename.
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
  python3 lib/config.py <config.yaml> get <key> [default]
  python3 lib/config.py <config.yaml> accounts
  python3 lib/config.py <config.yaml> first-account
  python3 lib/config.py <config.yaml> account <name> <key> [default]
  python3 lib/config.py <config.yaml> account-dirs <name>
  python3 lib/config.py <config.yaml> account-projects <name>
  python3 lib/config.py <config.yaml> project-dirs <name>
  python3 lib/config.py <config.yaml> project-account <name>
  python3 lib/config.py <config.yaml> digest-file      # today/tomorrow's digest
                                                       # path per digest_time
                                                       # (ORCH_NOW overrides
                                                       # "now", for tests)
"""
import os
import sys
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


CONFIG_NAMES = ("config.yaml", "config.yml")


def _first_existing(directory):
    """The first CONFIG_NAMES spelling present in `directory`, else None.
    `.yaml` wins when both exist: it is the preferred spelling, `.yml` is
    accepted so existing installs keep working without a rename."""
    for name in CONFIG_NAMES:
        candidate = Path(directory) / name
        if candidate.exists():
            return candidate
    return None


def resolve_path(root=None):
    """The live config file, in resolution order: ORCH_CONFIG env override,
    then <backlog root>/config.yaml (or .yml) when it exists, then the repo's
    example orchestrator/config.yaml under ORCH_EXAMPLE=1 only. `root`
    overrides backlog_root(). Raises BacklogRootError when the root has no
    config and the example was not asked for."""
    explicit = os.environ.get("ORCH_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    root = Path(root) if root is not None else backlog_root()
    found = _first_existing(root)
    if found is not None:
        return found
    if not example_requested():
        raise BacklogRootError(
            f"no config.yaml in backlog root {root} (set BACKLOG_ROOT to the "
            f"real backlog, or ORCH_EXAMPLE=1 for the repo's example config)")
    example = repo_root() / "orchestrator"
    return _first_existing(example) or example / CONFIG_NAMES[0]


def _coerce(raw):
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def split_values(raw):
    """A multi-value field (like a project's `dirs`) as a list of strings.

    Two spellings are accepted: YAML flow style `[a, b]` (preferred, and what
    the shipped config uses) and the legacy space-separated `a b`. Flow style is
    detected by the brackets, so a value containing spaces is only expressible
    with it - which is why it is preferred.
    """
    raw = str(raw).strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        return [v.strip() for v in inner.split(",") if v.strip()]
    return raw.split()


def _pair(stripped):
    """Split one "key: value" line; only the FIRST colon separates key from
    value, so "night_start: 02:00" works. Returns (key, value) or None."""
    key, _, raw = stripped.partition(":")
    key, raw = key.strip(), raw.strip()
    if not key:
        return None
    return key, raw


def load(path):
    cfg = {}
    section = None  # the list currently being filled, if any
    item = None     # the list item currently being filled, if any
    for raw_line in Path(path).read_text().splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            section = item = None
            pair = _pair(stripped)
            if pair is None:
                continue
            key, raw = pair
            if raw:
                cfg[key] = _coerce(raw)
            else:
                cfg[key] = []
                section = cfg[key]
        elif section is not None:
            if stripped.startswith("- "):
                item = {}
                section.append(item)
                stripped = stripped[2:].strip()
                if not stripped:
                    continue
            if item is None:
                continue
            pair = _pair(stripped)
            if pair and pair[1]:
                item[pair[0]] = _coerce(pair[1])
    return cfg


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
    raw = cfg.get("accounts") or [{"name": "default", "claude_config_dir": "~/.claude"}]
    out, seen = [], set()
    for a in raw:
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
PROJECT_KEYS = ("name", "account", "dirs", "rank", "class",
                "local_only_default")
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
    raw = cfg.get("projects")
    if not raw:
        raw = [{"name": "default", "account": accounts(cfg)[0]["name"],
                "dirs": cfg.get("extra_dirs", "")}]
    out = {}
    for p in raw:
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
        rank = p.get("rank", 100)
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise ValueError(f"config: project {p['name']!r} rank {rank!r} "
                             f"is not an integer")
        out[p["name"]] = {
            "name": p["name"],
            "account": p["account"],
            "dirs": [os.path.expanduser(d) for d in split_values(p.get("dirs", ""))],
            "rank": rank,
            "expedite": cls == "expedite",
            "local_only_default": bool(p.get("local_only_default", False)),
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
    if cmd == "account-dirs":
        seen = []
        for p in projects(cfg).values():
            if p["account"] == argv[3]:
                for d in p["dirs"]:
                    if d not in seen:
                        seen.append(d)
        print(" ".join(seen))
        return 0
    if cmd == "project-dirs":
        p = projects(cfg).get(argv[3])
        if p is None:
            print(f"config: unknown project {argv[3]!r}", file=sys.stderr)
            return 2
        print(" ".join(p["dirs"]))
        return 0
    if cmd == "project-local-only":
        p = projects(cfg).get(argv[3])
        if p is None:
            print(f"config: unknown project {argv[3]!r}", file=sys.stderr)
            return 2
        print(_fmt(p["local_only_default"]))
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
