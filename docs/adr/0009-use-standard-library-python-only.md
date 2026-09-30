# 0009. Use standard-library Python only, with a hand-rolled YAML subset for the config

- Status: accepted (retroactive)
- Date: 2026-09-04

## Context

The engine is installed with `git clone` and run unattended by launchd (`docs/design.md:80-81`).
A launchd job runs with a minimal PATH, and an interpreter upgrade can silently break a virtualenv or a `pip install`, with nobody at the keyboard to notice (maintainer's notes).
Any third-party package is therefore one more thing that can fail at night without being seen.
The config needs comments and a few lists (accounts, projects), and the standard library in the supported Python versions had no parser that offered both.
The decision is already in the initial public release (`568f33a`), which is the earliest date the public history shows.

## Decision

The engine uses the Python standard library only: no virtualenv, no pip, "nothing to install on the Python side" (`README.md:48-49`, `docs/design.md:81`).
As a consequence, the config is written in a small hand-rolled YAML subset, parsed by `orchestrator/lib/config.py`: top-level `key: value` scalars and top-level sections holding lists of flat mappings, with flow-style lists and no nesting, quoting or multi-line values (`orchestrator/config.yaml:1-11`, `orchestrator/lib/config.py:1-19`).

## Alternatives considered

- **A YAML library.** Rejected because it is a dependency, which the stdlib-only rule excludes (maintainer's notes).
- **JSON (`json` in the standard library).** Rejected because it has no comments, and the example config documents every knob in comments (maintainer's notes, `orchestrator/config.yaml`).
- **TOON.** Rejected because it is a prompt format, not a config format (maintainer's notes).
- **Strongest argument against.** The file looks like YAML but is not YAML: a user who writes valid YAML (quoting, nesting) gets a parse error or a silent misread, no editor or linter validates it, and every new config need reopens a parser the engine has to own and test.
  The stdlib-only constraint did not force this choice once `tomllib` exists in the standard library (Python 3.11+), which gives a real, specified format with comments at no dependency cost.

**Would we decide the same today?** No for the YAML subset - the file is not YAML and every new need reopens the parser; superseded by pull request #36 (TOML read by `tomllib`). Yes for stdlib-only - unattended launchd runs still make any dependency a silent failure mode.

## Consequences

- Good: install is a clone and nothing on the Python side can break between two nights; there is no environment to activate under launchd.
- Good: the parser is small and fully under the engine's control.
- Bad: the config format is a private dialect; it has to be documented in the file itself and every new shape needs parser work.
- Bad: stdlib-only rules out convenient libraries everywhere, not just for the config.
- The config-format half is superseded by pull request #36 ("Config: TOML via tomllib replaces the hand-rolled YAML subset") once it merges; the stdlib-only half stands.
- Tension to watch: `tomllib` is only in Python 3.11+, while the README and design doc state a Python 3.9+ floor (`README.md:48`, `docs/design.md:80`).
  PR #36 raises the floor to 3.11, so merging it drops support for 3.9 and 3.10 interpreters, including an older system `python3` that a launchd job may pick up by default.

## Sources

- `README.md:44`, `README.md:48-49` (dependency-free Python, Python 3.9+, nothing to install).
- `docs/design.md:80-81` (standard library only, no virtualenv, no pip).
- `orchestrator/config.yaml:1-11` (the format description), `orchestrator/lib/config.py:1-19` (the parser).
- Commit `568f33a` (initial public release).
- Pull request #36, "Config: TOML via tomllib replaces the hand-rolled YAML subset" (open; raises the Python floor to 3.11).
- Maintainer's notes (the unattended-install reasoning and the rejected alternatives).
