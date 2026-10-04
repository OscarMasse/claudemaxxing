import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from lib import config, tasks

NESTED = (
    "# comment\n"
    "dry_run = true\n"
    "night_start = \"02:00\"\n"
    "night_budget_ratio = 2.0\n"
    "claude_bin = \"/usr/local/bin/claude\"\n"
    "max_session_usd = 100\n"
    "[[accounts]]\n"
    "name = \"personal\"\n"
    "claude_config_dir = \"~/.claude\"\n"
    "max_session_usd = 300\n"
    "reset_tz = \"Europe/Warsaw\"\n"
    "reset_time = \"05:59\"\n"
    "[[accounts]]\n"
    "name = \"work\"\n"
    "claude_config_dir = \"~/.claude-work\"\n"
    "claude_bin = \"/opt/claude\"\n"
    "[[projects]]\n"
    "name = \"side-projects\"\n"
    "account = \"personal\"\n"
    "dirs = [\"~/projects\", \"~/oss\"]\n"
    "rank = 10\n"
    "[[projects]]\n"
    "name = \"job\"\n"
    "account = \"work\"\n"
    "dirs = [\"~/work\"]\n"
    "local_only_default = true\n"
)

MINIMAL = ('[[accounts]]\nname = "a"\n'
           '[[projects]]\nname = "p"\naccount = "a"\n')


def write_cfg(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


class TestScalarParsing(unittest.TestCase):
    def test_parses_types(self):
        cfg = config.load(write_cfg(
            "# comment\n"
            "dry_run = true\n"
            "max_session_usd = 300000000\n"
            "night_budget_ratio = 2.0\n"
            'reset_tz = "Europe/Warsaw"\n'
            'reset_time = "05:59"\n'
            'digest_time = "07:37"\n' + MINIMAL))
        self.assertIs(cfg["dry_run"], True)
        self.assertEqual(cfg["max_session_usd"], 300000000)
        self.assertAlmostEqual(cfg["night_budget_ratio"], 2.0)
        self.assertEqual(cfg["reset_tz"], "Europe/Warsaw")
        self.assertEqual(cfg["reset_time"], "05:59")
        self.assertEqual(cfg["digest_time"], "07:37")


class TestRejectedShapes(unittest.TestCase):
    """validate() refuses at load time every structure the engine ignores."""

    def assert_refused(self, text, pattern):
        with self.assertRaisesRegex(ValueError, pattern):
            config.load(write_cfg(text))

    def test_top_level_table_refused(self):
        self.assert_refused(MINIMAL + "[foo]\nbar = 1\n", "'foo' is a table")

    def test_table_inside_account_refused(self):
        self.assert_refused(
            '[[accounts]]\nname = "a"\n[accounts.caps]\nx = 1\n'
            '[[projects]]\nname = "p"\naccount = "a"\n', "'caps' is a table")

    def test_array_where_scalar_expected_refused(self):
        self.assert_refused('night_start = ["02:00"]\n' + MINIMAL,
                            "'night_start' is an array")

    def test_array_in_account_refused(self):
        self.assert_refused('[[accounts]]\nname = "a"\ndirs = ["~/x"]\n'
                            '[[projects]]\nname = "p"\naccount = "a"\n',
                            "'dirs' is an array")

    def test_non_string_dirs_refused(self):
        self.assert_refused(MINIMAL + "dirs = [1, 2]\n", "array of strings")

    def test_missing_accounts_refused(self):
        self.assert_refused('[[projects]]\nname = "p"\naccount = "a"\n',
                            r"no \[\[accounts\]\]")

    def test_missing_projects_refused(self):
        self.assert_refused('[[accounts]]\nname = "a"\n',
                            r"no \[\[projects\]\]")

    def test_unquoted_time_refused(self):
        self.assert_refused("reset_time = 05:59:00\n" + MINIMAL,
                            "'reset_time' has type time")

    def test_unknown_workday_refused(self):
        self.assert_refused('workdays = ["mon", "sat", "fry"]\n' + MINIMAL,
                            "unknown weekday 'fry'")

    def test_non_string_workdays_refused(self):
        self.assert_refused("workdays = [1]\n" + MINIMAL, "weekday names")
        self.assert_refused("workdays = []\n" + MINIMAL, "'workdays' is empty")

    def test_offday_ratio_out_of_range_refused(self):
        self.assert_refused("offday_reserve_ratio = 1.5\n" + MINIMAL, r"in \[0, 1\]")
        self.assert_refused('offday_reserve_ratio = "low"\n' + MINIMAL, r"in \[0, 1\]")
        self.assert_refused('[[accounts]]\nname = "a"\noffday_reserve_ratio = -0.1\n'
                            '[[projects]]\nname = "p"\naccount = "a"\n', r"in \[0, 1\]")

    def test_workdays_merge_into_accounts(self):
        cfg = config.load(write_cfg(
            'workdays = ["mon", "tue"]\noffday_reserve_ratio = 0\n'
            '[[accounts]]\nname = "a"\n[[accounts]]\nname = "b"\n'
            'workdays = ["sun"]\n[[projects]]\nname = "p"\naccount = "a"\n'))
        a, b = config.accounts(cfg)
        self.assertEqual(a["workdays"], ["mon", "tue"])
        self.assertEqual(a["offday_reserve_ratio"], 0)
        self.assertEqual(b["workdays"], ["sun"])

    def test_syntax_error_is_value_error(self):
        self.assert_refused("dry_run = \n" + MINIMAL, "config: ")

    def test_yaml_path_refused_with_migration_hint(self):
        path = Path(tempfile.mkdtemp()) / "config.yaml"
        path.write_text("dry_run: true\n")
        with self.assertRaisesRegex(config.ConfigFormatError,
                                    "config_yaml_to_toml.py"):
            config.load(path)


class TestSections(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(write_cfg(NESTED))

    def test_section_lists_parsed(self):
        self.assertEqual(len(self.cfg["accounts"]), 2)
        self.assertEqual(len(self.cfg["projects"]), 2)
        self.assertEqual(self.cfg["accounts"][0]["name"], "personal")
        self.assertEqual(self.cfg["accounts"][1]["claude_bin"], "/opt/claude")
        self.assertEqual(self.cfg["projects"][0]["rank"], 10)
        self.assertIs(self.cfg["projects"][1]["local_only_default"], True)

    def test_accounts_merge_flat_defaults(self):
        accts = config.accounts(self.cfg)
        personal, work = accts
        # Account key wins over the flat default.
        self.assertEqual(personal["max_session_usd"], 300)
        # Flat default inherited when the account does not override.
        self.assertEqual(work["max_session_usd"], 100)
        self.assertEqual(personal["claude_bin"], "/usr/local/bin/claude")
        self.assertEqual(work["claude_bin"], "/opt/claude")
        self.assertIs(personal["dry_run"], True)

    def test_account_config_dir_expanded(self):
        accts = config.accounts(self.cfg)
        self.assertEqual(accts[0]["claude_config_dir"],
                         os.path.expanduser("~/.claude"))
        self.assertEqual(accts[1]["claude_config_dir"],
                         os.path.expanduser("~/.claude-work"))

    def test_projects_registry(self):
        projs = config.projects(self.cfg)
        self.assertEqual(set(projs), {"side-projects", "job"})
        sp = projs["side-projects"]
        self.assertEqual(sp["account"], "personal")
        self.assertEqual(sp["dirs"], [os.path.expanduser("~/projects"),
                                      os.path.expanduser("~/oss")])
        self.assertEqual(sp["rank"], 10)
        self.assertIs(sp["expedite"], False)
        self.assertIs(sp["local_only_default"], False)
        self.assertEqual(projs["job"]["rank"], 100)  # default
        self.assertIs(projs["job"]["local_only_default"], True)

    def _project_cfg(self, extra):
        return config.load(write_cfg(MINIMAL + extra))

    def test_project_optional_dirs_default_empty_and_expand(self):
        self.assertEqual(config.projects(self._project_cfg(""))["p"]["optional_dirs"], [])
        projs = config.projects(self._project_cfg(
            'optional_dirs = ["~/projects/big-assets"]\n'))
        self.assertEqual(projs["p"]["optional_dirs"],
                         [os.path.expanduser("~/projects/big-assets")])

    def test_empty_dirs(self):
        self.assertEqual(config.projects(self._project_cfg("dirs = []\n"))["p"]["dirs"], [])

    def test_project_expedite_class(self):
        projs = config.projects(self._project_cfg('class = "expedite"\n'))
        self.assertIs(projs["p"]["expedite"], True)

    def test_project_retired_priority_refused(self):
        # The old hard first sort key must not be silently reinterpreted.
        with self.assertRaisesRegex(ValueError, "retired key 'priority'"):
            config.projects(self._project_cfg("priority = 5\n"))

    def test_project_unknown_key_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown key 'rnak'"):
            config.projects(self._project_cfg("rnak = 1\n"))

    def test_project_unknown_class_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown class 'expedit'"):
            config.projects(self._project_cfg('class = "expedit"\n'))

    def test_project_non_integer_rank_refused(self):
        with self.assertRaisesRegex(ValueError, "not an integer"):
            config.projects(self._project_cfg('rank = "high"\n'))

    def test_project_local_only_cli(self):
        # run.sh keys the agent GitHub token on this: a local-only project
        # must not get it (its reads fall back to the owner's keyring).
        path = write_cfg(NESTED)
        out = {}
        for name in ("side-projects", "job"):
            out[name] = subprocess.run(
                [sys.executable, str(Path(config.__file__)), path,
                 "project-local-only", name],
                capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, {"side-projects": "false", "job": "true"})

    def test_project_unknown_account_raises(self):
        cfg = config.load(write_cfg(
            '[[accounts]]\nname = "a"\n'
            '[[projects]]\nname = "p"\naccount = "nope"\n'))
        with self.assertRaises(ValueError):
            config.projects(cfg)

    def test_account_without_name_raises(self):
        cfg = config.load(write_cfg(
            '[[accounts]]\nclaude_config_dir = "~/.claude"\n'
            '[[projects]]\nname = "p"\naccount = "a"\n'))
        with self.assertRaises(ValueError):
            config.accounts(cfg)


class TestSplitValues(unittest.TestCase):
    """Frontmatter lists (tasks.py) accept flow style and space separation."""

    def test_split_values_directly(self):
        self.assertEqual(tasks.split_values("[a, b]"), ["a", "b"])
        self.assertEqual(tasks.split_values("a b"), ["a", "b"])
        self.assertEqual(tasks.split_values(""), [])
        self.assertEqual(tasks.split_values("[]"), [])
        # A lone bracket is not flow style and must not be mangled.
        self.assertEqual(tasks.split_values("[a"), ["[a"])


class TestMisconfiguredAccount(unittest.TestCase):
    """The budget unit is USD; a token-era key is refused, never converted."""

    GOOD = {"name": "a", "max_session_usd": 30}

    def test_an_account_without_caps_is_fine(self):
        self.assertIsNone(config.misconfigured_account(self.GOOD))

    def test_every_retired_key_is_refused(self):
        for key in config.RETIRED_KEYS:
            problem = config.misconfigured_account(dict(self.GOOD, **{key: 1}))
            self.assertEqual(
                problem, f"retired key {key}, {config.RETIRED_KEYS[key]}")

    def test_a_flat_retired_key_reaches_every_account(self):
        # Flat keys are inherited defaults, so one leftover at the top level
        # misconfigures every account that does not override it - which none
        # can, since the key itself is the problem.
        cfg = config.load(write_cfg(
            "est_session_tokens = 2500000\n"
            '[[accounts]]\nname = "a"\nmax_session_usd = 1\n'
            '[[accounts]]\nname = "b"\nmax_session_usd = 1\n'
            '[[projects]]\nname = "p"\naccount = "a"\n'))
        problems = [config.misconfigured_account(a) for a in config.accounts(cfg)]
        self.assertTrue(all(p and "est_session_tokens" in p for p in problems))


class TestResolution(unittest.TestCase):
    """resolve_path order: ORCH_CONFIG, then $BACKLOG_ROOT/config.toml,
    then the repo's example orchestrator/config.toml (ORCH_EXAMPLE=1 only).
    A retired config.yaml alone in the root fails loud with the migration
    command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Every test starts from a clean slate for the two env knobs.
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        os.environ.pop("ORCH_CONFIG", None)
        os.environ.pop("BACKLOG_ROOT", None)
        os.environ.pop("ORCH_ROOT", None)
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "xdg")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_orch_config_wins(self):
        explicit = self.root / "explicit.toml"
        explicit.write_text("dry_run = true\n")
        (self.root / "config.toml").write_text("dry_run = false\n")
        os.environ["ORCH_CONFIG"] = str(explicit)
        os.environ["BACKLOG_ROOT"] = str(self.root)
        self.assertEqual(config.resolve_path(), explicit)

    def test_backlog_root_config_next(self):
        (self.root / "config.toml").write_text("dry_run = true\n")
        os.environ["BACKLOG_ROOT"] = str(self.root)
        self.assertEqual(config.resolve_path(), self.root / "config.toml")

    def test_repo_default_last(self):
        os.environ["BACKLOG_ROOT"] = str(self.root)  # no config in it
        self.assertEqual(config.resolve_path(),
                         config.repo_root() / "orchestrator" / "config.toml")

    def test_yaml_alone_raises_with_migration_command(self):
        for name in ("config.yaml", "config.yml"):
            with self.subTest(name=name):
                (self.root / name).write_text("dry_run: true\n")
                os.environ["BACKLOG_ROOT"] = str(self.root)
                with self.assertRaisesRegex(config.ConfigFormatError,
                                            "scripts/config_yaml_to_toml.py"):
                    config.resolve_path()
                (self.root / name).unlink()

    def test_toml_wins_over_yaml_when_both_exist(self):
        (self.root / "config.toml").write_text("dry_run = true\n")
        (self.root / "config.yaml").write_text("dry_run: false\n")
        os.environ["BACKLOG_ROOT"] = str(self.root)
        self.assertEqual(config.resolve_path(), self.root / "config.toml")

    def test_explicit_root_argument_overrides_env(self):
        (self.root / "config.toml").write_text("dry_run = true\n")
        os.environ["BACKLOG_ROOT"] = "/nonexistent"
        self.assertEqual(config.resolve_path(self.root),
                         self.root / "config.toml")

    def test_repo_root_only_under_orch_example(self):
        self.assertEqual(config.backlog_root(), config.repo_root())
        os.environ.pop("ORCH_EXAMPLE")
        with self.assertRaisesRegex(config.BacklogRootError,
                                    "BACKLOG_ROOT.*backlog-root"):
            config.backlog_root()

    def test_root_order_orch_root_env_then_recorded_file(self):
        os.environ.pop("ORCH_EXAMPLE")
        recorded = self.root / "recorded"
        config.root_file().parent.mkdir(parents=True)
        config.root_file().write_text(f"{recorded}\n")
        self.assertEqual(config.backlog_root(), recorded)
        os.environ["BACKLOG_ROOT"] = str(self.root / "env")
        self.assertEqual(config.backlog_root(), self.root / "env")
        os.environ["ORCH_ROOT"] = str(self.root / "orch")
        self.assertEqual(config.backlog_root(), self.root / "orch")

    def test_root_without_config_fails_outside_example(self):
        os.environ.pop("ORCH_EXAMPLE")
        os.environ["BACKLOG_ROOT"] = str(self.root)  # no config in it
        with self.assertRaisesRegex(config.BacklogRootError, "no config.toml"):
            config.resolve_path()

    def test_record_root_cli_round_trips(self):
        os.environ.pop("ORCH_EXAMPLE")
        cli = [sys.executable, str(config.repo_root() / "orchestrator" / "lib" / "config.py")]
        subprocess.run(cli + ["record-root", str(self.root)], check=True,
                       capture_output=True)
        out = subprocess.run(cli + ["backlog-root"], check=True,
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(Path(out), self.root.resolve())


class TestEntryPointsFailLoudly(unittest.TestCase):
    """With no ORCH_ROOT, BACKLOG_ROOT, recorded file nor ORCH_EXAMPLE, every
    entry point exits non-zero naming the variable and the file, instead of
    reading the repo's example config (2026-09-26 fake alarms)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("ORCH_ROOT", "BACKLOG_ROOT", "ORCH_EXAMPLE",
                                 "ORCH_CONFIG")}
        self.env["XDG_CONFIG_HOME"] = self.tmp.name
        self.orch = config.repo_root() / "orchestrator"

    def tearDown(self):
        self.tmp.cleanup()

    def assert_fails_loudly(self, cmd):
        r = subprocess.run(cmd, cwd=self.orch, env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("BACKLOG_ROOT", r.stderr)
        self.assertIn(str(Path(self.tmp.name) / "claudemaxxing" / "backlog-root"),
                      r.stderr)
        self.assertNotIn("account=", r.stdout)

    def test_gate_status(self):
        self.assert_fails_loudly([sys.executable, "gate.py", "status"])

    def test_manual_py(self):
        self.assert_fails_loudly([sys.executable, "manual.py", "--count", "1",
                                  "--dry-run"])

    def test_shell_entry_points(self):
        for script in ("run.sh", "manual.sh", "digest-wrapper.sh",
                       "gatekeeper.sh"):
            with self.subTest(script=script):
                args = ["--count", "1", "--dry-run"] if script == "manual.sh" else []
                self.assert_fails_loudly(["/bin/bash", script] + args)

    def test_gate_status_prints_backlog_first_when_recorded(self):
        root = Path(self.tmp.name) / "backlog"
        root.mkdir()
        (root / "config.toml").write_text(
            (self.orch / "config.toml").read_text())
        subprocess.run([sys.executable, "lib/config.py", "record-root", str(root)],
                       cwd=self.orch, env=self.env, check=True, capture_output=True)
        r = subprocess.run([sys.executable, "gate.py", "status"], cwd=self.orch,
                           env=dict(self.env, ORCH_NO_NOTIFY="1"),
                           capture_output=True, text=True, timeout=30)
        self.assertTrue(r.stdout.startswith(f"backlog={root.resolve()} "), r.stdout)


class TestDigestFileCLI(unittest.TestCase):
    """`config.py <cfg> digest-file` as run.sh actually invokes it: as a
    subprocess, under an external BACKLOG_ROOT."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = self.root / "config.toml"
        self.cfg.write_text('digest_time = "07:37"\n' + MINIMAL)
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
        cfg = config.load(Path(__file__).resolve().parents[1] / "config.toml")
        for key in ("dry_run", "night_start", "night_end", "morning_guard",
                    "prereset_burn_hours",
                    "activity_idle_night_min", "night_slice_min",
                    "claude_bin", "claude_model",
                    "claude_effort", "max_session_usd", "digest_time"):
            self.assertIn(key, cfg)
        accts = config.accounts(cfg)
        self.assertGreaterEqual(len(accts), 2)
        for a in accts:
            for key in ("name", "claude_config_dir", "reset_weekday",
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
