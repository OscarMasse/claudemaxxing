import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from lib import config

NESTED = (
    "# comment\n"
    "dry_run: true\n"
    "night_start: 02:00\n"
    "night_budget_ratio: 2.0\n"
    "claude_bin: /usr/local/bin/claude\n"
    "weekly_cap_usd: 100\n"
    "accounts:\n"
    "  - name: personal\n"
    "    claude_config_dir: ~/.claude\n"
    "    weekly_cap_usd: 300\n"
    "    reset_tz: Europe/Warsaw\n"
    "    reset_time: 05:59\n"
    "  - name: work\n"
    "    claude_config_dir: ~/.claude-work\n"
    "    claude_bin: /opt/claude\n"
    "    promo_until: 2026-08-19\n"
    "projects:\n"
    "  - name: side-projects\n"
    "    account: personal\n"
    "    dirs: ~/projects ~/oss\n"
    "    priority: 10\n"
    "  - name: job\n"
    "    account: work\n"
    "    dirs: ~/work\n"
    "    local_only_default: true\n"
)


def write_cfg(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


class TestFlatParsing(unittest.TestCase):
    def test_parses_types(self):
        cfg = config.load(write_cfg(
            "# comment\n"
            "dry_run: true\n"
            "weekly_cap_usd: 300000000\n"
            "night_budget_ratio: 2.0\n"
            "reset_tz: Europe/Warsaw\n"
            "reset_time: 05:59\n"
            "promo_until: 2026-08-19\n"))
        self.assertIs(cfg["dry_run"], True)
        self.assertEqual(cfg["weekly_cap_usd"], 300000000)
        self.assertAlmostEqual(cfg["night_budget_ratio"], 2.0)
        self.assertEqual(cfg["reset_tz"], "Europe/Warsaw")
        self.assertEqual(cfg["reset_time"], "05:59")
        self.assertEqual(cfg["promo_until"], "2026-08-19")


class TestSections(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(write_cfg(NESTED))

    def test_section_lists_parsed(self):
        self.assertEqual(len(self.cfg["accounts"]), 2)
        self.assertEqual(len(self.cfg["projects"]), 2)
        self.assertEqual(self.cfg["accounts"][0]["name"], "personal")
        self.assertEqual(self.cfg["accounts"][1]["claude_bin"], "/opt/claude")
        self.assertEqual(self.cfg["projects"][0]["priority"], 10)
        self.assertIs(self.cfg["projects"][1]["local_only_default"], True)

    def test_scalar_after_section_returns_to_top_level(self):
        cfg = config.load(write_cfg(NESTED + "max_parallel_sessions: 3\n"))
        self.assertEqual(cfg["max_parallel_sessions"], 3)
        self.assertEqual(len(cfg["accounts"]), 2)

    def test_accounts_merge_flat_defaults(self):
        accts = config.accounts(self.cfg)
        personal, work = accts
        # Account key wins over the flat default.
        self.assertEqual(personal["weekly_cap_usd"], 300)
        # Flat default inherited when the account does not override.
        self.assertEqual(work["weekly_cap_usd"], 100)
        self.assertEqual(personal["claude_bin"], "/usr/local/bin/claude")
        self.assertEqual(work["claude_bin"], "/opt/claude")
        self.assertIs(personal["dry_run"], True)

    def test_account_config_dir_expanded(self):
        accts = config.accounts(self.cfg)
        self.assertEqual(accts[0]["claude_config_dir"],
                         os.path.expanduser("~/.claude"))
        self.assertEqual(accts[1]["claude_config_dir"],
                         os.path.expanduser("~/.claude-work"))

    def test_account_promo_defaults(self):
        accts = config.accounts(self.cfg)
        self.assertEqual(accts[0]["promo_multiplier"], 1.0)
        self.assertEqual(accts[0]["promo_until"], "2000-01-01")
        self.assertEqual(accts[1]["promo_until"], "2026-08-19")

    def test_projects_registry(self):
        projs = config.projects(self.cfg)
        self.assertEqual(set(projs), {"side-projects", "job"})
        sp = projs["side-projects"]
        self.assertEqual(sp["account"], "personal")
        self.assertEqual(sp["dirs"], [os.path.expanduser("~/projects"),
                                      os.path.expanduser("~/oss")])
        self.assertEqual(sp["priority"], 10)
        self.assertIs(sp["local_only_default"], False)
        self.assertEqual(projs["job"]["priority"], 100)  # default
        self.assertIs(projs["job"]["local_only_default"], True)

    def test_project_unknown_account_raises(self):
        cfg = config.load(write_cfg(
            "accounts:\n  - name: a\n    claude_config_dir: ~/.claude\n"
            "projects:\n  - name: p\n    account: nope\n"))
        with self.assertRaises(ValueError):
            config.projects(cfg)

    def test_account_without_name_raises(self):
        cfg = config.load(write_cfg("accounts:\n  - claude_config_dir: ~/.claude\n"))
        with self.assertRaises(ValueError):
            config.accounts(cfg)


class TestFlowStyleLists(unittest.TestCase):
    """Multi-value fields accept YAML flow style [a, b]; the legacy
    space-separated form keeps parsing."""

    def dirs_for(self, dirs_line):
        cfg = config.load(write_cfg(
            "accounts:\n  - name: personal\n    claude_config_dir: ~/.claude\n"
            f"projects:\n  - name: p\n    account: personal\n    {dirs_line}\n"))
        return config.projects(cfg)["p"]["dirs"]

    def test_flow_style_list(self):
        self.assertEqual(self.dirs_for("dirs: [~/one, ~/two]"),
                         [os.path.expanduser("~/one"),
                          os.path.expanduser("~/two")])

    def test_space_separated_still_parses(self):
        self.assertEqual(self.dirs_for("dirs: ~/one ~/two"),
                         [os.path.expanduser("~/one"),
                          os.path.expanduser("~/two")])

    def test_single_entry_and_whitespace_tolerance(self):
        self.assertEqual(self.dirs_for("dirs: [ ~/one ]"),
                         [os.path.expanduser("~/one")])

    def test_empty_list(self):
        self.assertEqual(self.dirs_for("dirs: []"), [])

    def test_split_values_directly(self):
        self.assertEqual(config.split_values("[a, b]"), ["a", "b"])
        self.assertEqual(config.split_values("a b"), ["a", "b"])
        self.assertEqual(config.split_values(""), [])
        self.assertEqual(config.split_values("[]"), [])
        # A lone bracket is not flow style and must not be mangled.
        self.assertEqual(config.split_values("[a"), ["[a"])


class TestMisconfiguredAccount(unittest.TestCase):
    """The budget unit is USD; a token-era key is refused, never converted."""

    GOOD = {"name": "a", "weekly_cap_usd": 780, "window_cap_usd": 67,
            "p90_daily_usd": 105}

    def test_a_complete_usd_account_is_fine(self):
        self.assertIsNone(config.misconfigured_account(self.GOOD))

    def test_every_retired_key_is_refused(self):
        for key in config.RETIRED_KEYS:
            problem = config.misconfigured_account(dict(self.GOOD, **{key: 1}))
            self.assertEqual(
                problem, f"retired key {key}, the unit is USD since 2026-09-12 "
                         "(see specs)")

    def test_a_missing_usd_cap_is_an_error_not_a_default(self):
        acct = dict(self.GOOD)
        del acct["weekly_cap_usd"]
        self.assertEqual(config.misconfigured_account(acct),
                         "missing key weekly_cap_usd, the unit is USD since "
                         "2026-09-12 (see specs)")

    def test_a_flat_retired_key_reaches_every_account(self):
        # Flat keys are inherited defaults, so one leftover at the top level
        # misconfigures every account that does not override it - which none
        # can, since the key itself is the problem.
        cfg = config.load(write_cfg(
            "est_session_tokens: 2500000\n"
            "accounts:\n"
            "  - name: a\n    weekly_cap_usd: 1\n    window_cap_usd: 1\n"
            "    p90_daily_usd: 1\n"
            "  - name: b\n    weekly_cap_usd: 1\n    window_cap_usd: 1\n"
            "    p90_daily_usd: 1\n"))
        problems = [config.misconfigured_account(a) for a in config.accounts(cfg)]
        self.assertTrue(all(p and "est_session_tokens" in p for p in problems))


class TestLegacyFallback(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(write_cfg(
            "weekly_cap_usd: 100\np90_daily_usd: 10\n"
            "reset_tz: Europe/Warsaw\nclaude_bin: /usr/local/bin/claude\n"
            "extra_dirs: ~/projects ~/work\n"))

    def test_default_account_synthesized(self):
        accts = config.accounts(self.cfg)
        self.assertEqual(len(accts), 1)
        self.assertEqual(accts[0]["name"], "default")
        self.assertEqual(accts[0]["claude_config_dir"],
                         os.path.expanduser("~/.claude"))
        self.assertEqual(accts[0]["weekly_cap_usd"], 100)

    def test_default_project_synthesized_from_extra_dirs(self):
        projs = config.projects(self.cfg)
        self.assertEqual(list(projs), ["default"])
        self.assertEqual(projs["default"]["account"], "default")
        self.assertEqual(projs["default"]["dirs"],
                         [os.path.expanduser("~/projects"),
                          os.path.expanduser("~/work")])


class TestResolution(unittest.TestCase):
    """resolve_path order: ORCH_CONFIG, then $BACKLOG_ROOT/config.yaml,
    then the repo's example orchestrator/config.yaml. Both the `.yaml` and the
    legacy `.yml` spelling are accepted at every step, `.yaml` preferred."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Every test starts from a clean slate for the two env knobs.
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        os.environ.pop("ORCH_CONFIG", None)
        os.environ.pop("BACKLOG_ROOT", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_orch_config_wins(self):
        explicit = self.root / "explicit.yml"
        explicit.write_text("dry_run: true\n")
        (self.root / "config.yml").write_text("dry_run: false\n")
        os.environ["ORCH_CONFIG"] = str(explicit)
        os.environ["BACKLOG_ROOT"] = str(self.root)
        self.assertEqual(config.resolve_path(), explicit)

    def test_backlog_root_config_next(self):
        (self.root / "config.yml").write_text("dry_run: true\n")
        os.environ["BACKLOG_ROOT"] = str(self.root)
        self.assertEqual(config.resolve_path(), self.root / "config.yml")

    def test_repo_default_last(self):
        os.environ["BACKLOG_ROOT"] = str(self.root)  # no config in it
        self.assertEqual(config.resolve_path(),
                         config.repo_root() / "orchestrator" / "config.yaml")

    def test_yaml_extension_is_found(self):
        (self.root / "config.yaml").write_text("dry_run: true\n")
        os.environ["BACKLOG_ROOT"] = str(self.root)
        self.assertEqual(config.resolve_path(), self.root / "config.yaml")

    def test_yaml_wins_over_yml_when_both_exist(self):
        (self.root / "config.yaml").write_text("dry_run: true\n")
        (self.root / "config.yml").write_text("dry_run: false\n")
        os.environ["BACKLOG_ROOT"] = str(self.root)
        self.assertEqual(config.resolve_path(), self.root / "config.yaml")

    def test_explicit_root_argument_overrides_env(self):
        (self.root / "config.yml").write_text("dry_run: true\n")
        os.environ["BACKLOG_ROOT"] = "/nonexistent"
        self.assertEqual(config.resolve_path(self.root),
                         self.root / "config.yml")

    def test_backlog_root_defaults_to_repo_root(self):
        self.assertEqual(config.backlog_root(), config.repo_root())
        os.environ["BACKLOG_ROOT"] = str(self.root)
        self.assertEqual(config.backlog_root(), self.root)


class TestDigestFileCLI(unittest.TestCase):
    """`config.py <cfg> digest-file` as run.sh actually invokes it: as a
    subprocess, under an external BACKLOG_ROOT."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = self.root / "config.yml"
        self.cfg.write_text("digest_time: 07:37\n")
        self.config_py = Path(__file__).resolve().parents[1] / "lib" / "config.py"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra_args, env_extra=None):
        env = dict(os.environ, BACKLOG_ROOT=str(self.root))
        env.pop("ORCH_CONFIG", None)
        if env_extra:
            env.update(env_extra)
        result = subprocess.run(
            [sys.executable, str(self.config_py), str(self.cfg), "digest-file", *extra_args],
            capture_output=True, text=True, env=env, check=True)
        return result.stdout.strip()

    def test_before_digest_time_lands_in_external_root_today(self):
        out = self.run_cli(env_extra={"ORCH_NOW": "2026-09-04T03:00:00"})
        self.assertEqual(out, str(self.root / "digests" / "2026-09-04.md"))

    def test_at_or_after_digest_time_lands_tomorrow(self):
        out = self.run_cli(env_extra={"ORCH_NOW": "2026-09-04T07:37:00"})
        self.assertEqual(out, str(self.root / "digests" / "2026-09-05.md"))

    def test_explicit_now_argument_used_when_no_orch_now_env(self):
        env = dict(os.environ, BACKLOG_ROOT=str(self.root))
        env.pop("ORCH_NOW", None)
        env.pop("ORCH_CONFIG", None)
        result = subprocess.run(
            [sys.executable, str(self.config_py), str(self.cfg), "digest-file",
             "2026-09-04T00:00:00"],
            capture_output=True, text=True, env=env, check=True)
        self.assertEqual(result.stdout.strip(),
                         str(self.root / "digests" / "2026-09-04.md"))


class TestRealConfig(unittest.TestCase):
    def test_real_config_shape(self):
        cfg = config.load(Path(__file__).resolve().parents[1] / "config.yaml")
        for key in ("dry_run", "night_start", "night_end", "morning_guard",
                    "prereset_burn_hours",
                    "activity_idle_night_min", "night_slice_min",
                    "claude_bin", "claude_model",
                    "claude_effort", "max_session_usd", "digest_time"):
            self.assertIn(key, cfg)
        accts = config.accounts(cfg)
        self.assertGreaterEqual(len(accts), 2)
        for a in accts:
            for key in ("name", "claude_config_dir", "weekly_cap_usd",
                        "window_cap_usd", "p90_daily_usd", "reset_weekday",
                        "reset_time", "reset_tz"):
                self.assertIn(key, a, f"account {a.get('name')} missing {key}")
            self.assertIsNone(config.misconfigured_account(a), a.get("name"))
        names = {a["name"] for a in accts}
        projs = config.projects(cfg)
        self.assertGreaterEqual(len(projs), 3)
        for p in projs.values():
            self.assertIn(p["account"], names)


if __name__ == "__main__":
    unittest.main()
