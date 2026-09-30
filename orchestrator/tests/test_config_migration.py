"""scripts/config_yaml_to_toml.py: the one-shot config.yaml -> config.toml
rewrite, checked against lib.config.load on the result."""
import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from lib import config

SCRIPT = config.repo_root() / "scripts" / "config_yaml_to_toml.py"
_spec = importlib.util.spec_from_file_location("config_yaml_to_toml", SCRIPT)
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)

YAML = (
    "# Shared knobs.\n"
    "dry_run: false  # flip to true to rehearse\n"
    "night_start: 02:00\n"
    "night_budget_ratio: 2.5\n"
    "max_parallel_sessions: 3\n"
    "claude_bin: /usr/local/bin/claude\n"
    "\n"
    "accounts:  # one per subscription\n"
    "  - name: personal\n"
    "    claude_config_dir: ~/.claude\n"
    "    reset_time: 05:59\n"
    "  - name: work\n"
    "    claude_config_dir: ~/.claude-work\n"
    "    reset_weekday: 3\n"
    "projects:\n"
    "  # the side projects\n"
    "  - name: side\n"
    "    account: personal\n"
    "    dirs: [~/projects, ~/oss]\n"
    "    optional_dirs: [~/assets]\n"
    "    rank: 10\n"
    "  - name: job\n"
    "    account: work\n"
    "    dirs: ~/work\n"
    "    local_only_default: true\n"
)


class TestConvert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = migrate.main(["x", str(self.root)])
        return rc, out.getvalue() + err.getvalue()

    def test_converted_config_loads_with_expected_values(self):
        (self.root / "config.yaml").write_text(YAML)
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        cfg = config.load(self.root / "config.toml")
        self.assertIs(cfg["dry_run"], False)
        self.assertEqual(cfg["night_start"], "02:00")
        self.assertEqual(cfg["night_budget_ratio"], 2.5)
        self.assertEqual(cfg["max_parallel_sessions"], 3)
        self.assertEqual(cfg["claude_bin"], "/usr/local/bin/claude")
        self.assertEqual(cfg["accounts"], [
            {"name": "personal", "claude_config_dir": "~/.claude",
             "reset_time": "05:59"},
            {"name": "work", "claude_config_dir": "~/.claude-work",
             "reset_weekday": 3}])
        self.assertEqual(cfg["projects"], [
            {"name": "side", "account": "personal",
             "dirs": ["~/projects", "~/oss"], "optional_dirs": ["~/assets"],
             "rank": 10},
            {"name": "job", "account": "work", "dirs": ["~/work"],
             "local_only_default": True}])
        projs = config.projects(cfg)
        self.assertEqual(projs["job"]["dirs"], [os.path.expanduser("~/work")])
        # The old file stays until the owner deletes it.
        self.assertTrue((self.root / "config.yaml").exists())

    def test_comments_are_preserved(self):
        text = migrate.convert(YAML)
        for comment in ("# Shared knobs.", "# flip to true to rehearse",
                        "# one per subscription", "# the side projects"):
            self.assertIn(comment, text)
        self.assertIn("dry_run = false # flip to true to rehearse", text)

    def test_refuses_when_config_toml_exists(self):
        (self.root / "config.yaml").write_text(YAML)
        (self.root / "config.toml").write_text("dry_run = true\n")
        rc, out = self.run_main()
        self.assertEqual(rc, 2)
        self.assertIn("already exists", out)
        self.assertEqual((self.root / "config.toml").read_text(),
                         "dry_run = true\n")

    def test_top_level_key_after_a_section_fails(self):
        with self.assertRaisesRegex(ValueError, "'dry_run' follows the "
                                                "'projects' section"):
            migrate.convert(YAML + "dry_run: true\n")

    def test_no_yaml_is_an_error(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 2)
        self.assertIn("no config.yaml", out)


if __name__ == "__main__":
    unittest.main()
