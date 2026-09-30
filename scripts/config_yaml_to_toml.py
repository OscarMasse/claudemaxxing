#!/usr/bin/env python3
"""One-shot migration: rewrite a backlog's config.yaml as config.toml.

    python3 scripts/config_yaml_to_toml.py ~/backlog

Reads `<root>/config.yaml` (or `.yml`) with the retired hand-rolled parser,
vendored below and nowhere else, and writes `<root>/config.toml` next to it.
The rewrite is line by line so every comment stays where it was: a top-level
`key: value` becomes `key = value`, each `- ` list item under `accounts:` or
`projects:` opens a `[[accounts]]` / `[[projects]]` table, and flow lists
become TOML arrays. Strings are quoted, so times like 05:59 stay strings.

Before writing, the result is parsed back with tomllib and compared with what
the old parser read; any difference aborts without touching the disk. The old
file is left in place: delete it once `gate.py status` looks right.
"""
import json
import sys
import tomllib
from pathlib import Path

LIST_KEYS = ("dirs", "optional_dirs")


# --- The retired parser (vendored verbatim, do not reuse elsewhere) ---------

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
    raw = str(raw).strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        return [v.strip() for v in inner.split(",") if v.strip()]
    return raw.split()


def _pair(stripped):
    key, _, raw = stripped.partition(":")
    key, raw = key.strip(), raw.strip()
    if not key:
        return None
    return key, raw


def load_yaml_subset(path):
    cfg = {}
    section = None
    item = None
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


# --- The rewrite -------------------------------------------------------------

def _line_pair(stripped):
    pair = _pair(stripped)
    if pair is None or ":" not in stripped:
        raise ValueError(f"line is not `key: value`: {stripped!r}")
    return pair


def _toml_value(key, raw):
    if key in LIST_KEYS:
        return "[" + ", ".join(json.dumps(v) for v in split_values(raw)) + "]"
    value = _coerce(raw)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value)


def convert(text):
    """The TOML rewrite of a config.yaml text, comments kept in place."""
    out = []
    section = None
    for raw_line in text.splitlines():
        code, hash_, comment = raw_line.partition("#")
        tail = (" " if code.strip() else "") + hash_ + comment if hash_ else ""
        if not code.strip():
            out.append(raw_line.rstrip())
            continue
        indent = len(code) - len(code.lstrip())
        stripped = code.strip()
        if indent == 0:
            key, raw = _line_pair(stripped)
            if raw:
                if section is not None:
                    raise ValueError(
                        f"top-level key {key!r} follows the {section!r} "
                        f"section; move it above the first section")
                out.append(f"{key} = {_toml_value(key, raw)}{tail}")
            else:
                section = key
                if tail:
                    out.append(tail.strip())
            continue
        if section is None:
            raise ValueError(f"indented line outside a section: {raw_line!r}")
        if stripped.startswith("- "):
            out.append(f"[[{section}]]")
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        key, raw = _line_pair(stripped)
        out.append(f"{key} = {_toml_value(key, raw)}{tail}")
    return "\n".join(out) + "\n"


def _normalized(cfg):
    """The old parser's dict with list fields split, as tomllib reads them."""
    norm = {}
    for key, value in cfg.items():
        if isinstance(value, list):
            norm[key] = [{k: split_values(v) if k in LIST_KEYS else v
                          for k, v in item.items()} for item in value]
        else:
            norm[key] = value
    return norm


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    root = Path(argv[1]).expanduser()
    source = next((root / n for n in ("config.yaml", "config.yml")
                   if (root / n).exists()), None)
    if source is None:
        print(f"no config.yaml or config.yml in {root}", file=sys.stderr)
        return 2
    target = root / "config.toml"
    if target.exists():
        print(f"{target} already exists, refusing to overwrite", file=sys.stderr)
        return 2
    try:
        text = convert(source.read_text())
    except ValueError as e:
        print(f"{source}: {e}; nothing written", file=sys.stderr)
        return 1
    if tomllib.loads(text) != _normalized(load_yaml_subset(source)):
        print("the TOML rewrite does not read back as the old config; "
              "nothing written", file=sys.stderr)
        return 1
    target.write_text(text)
    print(f"wrote {target}; delete {source} once `gate.py status` looks right")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
