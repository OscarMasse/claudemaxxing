# 0009. Use standard-library Python only, with a hand-rolled YAML subset for the config

- Status: accepted (retroactive)
- Date: 2026-08-12

## Context

The engine is installed with `git clone` and run unattended by launchd (`docs/design.md:81-88`, launchd install at `:88`).
A launchd job runs with a minimal PATH, and an interpreter upgrade can silently break a virtualenv or a `pip install`, with nobody at the keyboard to notice (maintainer's notes).
Any third-party package is therefore one more thing that can fail at night without being seen.
The config needs comments and a few lists (accounts, projects).
The maintainer's implementation plan of 2026-08-12 already fixes both halves: standard library only with no pip installs, and a flat `key: value` config read by a hand-written parser with no YAML library (maintainer's notes).
Both are in the initial public release (`568f33a`, 2026-09-04), which required Python 3.11+ (`README.md` at `568f33a`).
Why TOML (`tomllib`, in the standard library since Python 3.11) was not chosen at the time is not recorded.

## Decision

The engine uses the Python standard library only: no virtualenv, no pip, "nothing to install on the Python side" (`README.md:48-49`, `docs/design.md:81`).
As a consequence, the config is written in a small hand-rolled YAML subset, parsed by `orchestrator/lib/config.py`: top-level `key: value` scalars and top-level sections holding lists of flat mappings, with flow-style lists at HEAD (added by `07d8e6e`, 2026-09-06) and no nesting, quoting or multi-line values (`orchestrator/config.yaml:1-11`, `orchestrator/lib/config.py:1-19`).

## Alternatives considered

- **A YAML library.** Rejected because it is a dependency, which the stdlib-only rule excludes (maintainer's notes).
- **JSON (`json` in the standard library).** Rejected because it has no comments, and the example config documents every knob in comments (maintainer's notes, `orchestrator/config.yaml`).
- **TOON.** Rejected because it is a prompt format, not a config format (maintainer's notes).
- **Strongest argument against.** (Own analysis.) The file looks like YAML but is not YAML: a user who writes valid YAML (quoting, nesting) gets a parse error or a silent misread, no editor or linter validates it, and every new config need reopens a parser the engine has to own and test.
  The stdlib-only constraint never forced this choice: `tomllib` was already in the standard library at the release's Python 3.11+ floor, so a real, specified format with comments was available at no dependency cost and the hand-rolled parser was avoidable from the start.

**Would we decide the same today?** No for the YAML subset - the file is not YAML and every new need reopens the parser; to be superseded by pull request #36 once it merges (TOML read by `tomllib`).
Yes for stdlib-only - unattended launchd runs still make any dependency a silent failure mode.

## Consequences

- Good: install is a clone and nothing on the Python side can break between two nights; there is no environment to activate under launchd.
- Good: the parser is small and fully under the engine's control.
- Bad: the config format is a private dialect; it has to be documented in the file itself and every new shape needs parser work.
- Bad: stdlib-only rules out convenient libraries everywhere, not just for the config.
- The config-format half is superseded by pull request #36 ("Config: TOML via tomllib replaces the hand-rolled YAML subset") once it merges; the stdlib-only half stands.
- PR #36 also deletes the legacy flat config format (no sections, `extra_dirs`), which affects the legacy flat format of `568f33a` and the flow-style lists of `07d8e6e`.
- Tension to watch: the Python floor was 3.11+ from the first release until `ef7c5b9` lowered it to 3.9 on 2026-09-30 (`README.md:48`, `docs/design.md:80`).
  PR #36 restores the 3.11 floor that `tomllib` needs, so merging it drops 3.9 and 3.10 again, including an older system `python3` that a launchd job may pick up by default.

## Sources

- `README.md:44`, `README.md:48-49` (dependency-free Python, Python 3.9+, nothing to install).
- `docs/design.md:80-83` (Python floor, standard library only, `git clone` install).
- `orchestrator/config.yaml:1-11` (the format description), `orchestrator/lib/config.py:1-19` (the parser).
- Commit `568f33a` (initial public release, Python 3.11+), `07d8e6e` (flow-style lists), `ef7c5b9` (floor lowered to 3.9).
- Pull request #36, "Config: TOML via tomllib replaces the hand-rolled YAML subset" (open; restores the Python 3.11 floor, deletes the legacy flat format).
- Maintainer's notes (the 2026-08-12 implementation plan, the unattended-install reasoning and the rejected alternatives).
