"""Guards against documentation drifting away from the code.

Every stale-docs problem this project has had was one of these: a setting the
code reads but no shipped config mentions, a subcommand missing from the
summary, or a flag documented with a name argparse does not accept. Each is
cheap to check mechanically, so none of them should need noticing by eye again.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from btcli.config import DEFAULT_SETTINGS
from btcli.fix import AVAILABLE_FIXES

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SHIPPED_CONF = ROOT / "settings.default.conf"

SUBCOMMANDS = ["probe", "translate", "fix", "interactive", "prune", "update"]


def load_shipped() -> dict:
    raw = SHIPPED_CONF.read_text(encoding="utf-8")
    cleaned = re.sub(r'(?m)^\s*//.*$', '', raw)
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
    return json.loads(cleaned)


def readme() -> str:
    return README.read_text(encoding="utf-8")


# ── settings ──────────────────────────────────────────────────────────────────

def test_shipped_config_is_valid_json_once_comments_are_stripped():
    assert load_shipped(), "settings.default.conf must parse"


def test_every_setting_the_code_knows_is_shipped():
    """A setting missing here can never reach a user's config.

    btcli update only adds keys that exist in settings.default.conf, so an
    unshipped setting is invisible even though the code reads it.
    """
    missing = sorted(set(DEFAULT_SETTINGS) - set(load_shipped()))
    assert not missing, f"absent from settings.default.conf: {missing}"


def test_shipped_config_invents_no_settings():
    unknown = sorted(set(load_shipped()) - set(DEFAULT_SETTINGS))
    assert not unknown, f"shipped but unknown to the code: {unknown}"


def test_shipped_values_match_the_built_in_defaults():
    """Otherwise the documented default and the actual default disagree."""
    shipped = load_shipped()
    differing = {k: (shipped[k], DEFAULT_SETTINGS[k])
                 for k in shipped if shipped[k] != DEFAULT_SETTINGS[k]}
    assert not differing, f"shipped != built-in: {differing}"


@pytest.mark.parametrize("setting", sorted(DEFAULT_SETTINGS))
def test_every_setting_is_named_in_the_readme(setting):
    assert setting in readme(), f"{setting} is undocumented in README.md"


# ── the CLI surface ───────────────────────────────────────────────────────────

def run_cli(*args):
    result = subprocess.run(
        [sys.executable, "-m", "btcli", *args],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    return result.stdout + result.stderr


@pytest.mark.parametrize("command", SUBCOMMANDS)
def test_every_subcommand_is_in_the_readme(command):
    assert f"btcli {command}" in readme(), f"README never shows 'btcli {command}'"


def commands_block() -> str:
    """The 'Commands:' listing from the no-command summary, and only that.

    Checked in isolation because a command name also appears further down in
    the usage examples, so searching the whole summary would pass even with the
    command absent from the listing.
    """
    summary = run_cli()
    assert "Commands:" in summary, "the no-command summary lost its listing"
    after = summary.split("Commands:", 1)[1]
    return after.split("\n\n", 1)[0]


@pytest.mark.parametrize("command", SUBCOMMANDS)
def test_every_subcommand_is_listed_in_the_no_command_summary(command):
    """The summary is what a user sees when they type btcli and nothing else."""
    assert command in commands_block(), \
        f"'{command}' is missing from the Commands: listing"


@pytest.mark.parametrize("fix", AVAILABLE_FIXES)
def test_every_fix_is_documented(fix):
    assert fix in readme(), f"fix '{fix}' is undocumented in README.md"
    assert fix in run_cli("fix", "-h"), f"fix '{fix}' is missing from fix -h"


@pytest.mark.parametrize("flag", [
    "--dry-run", "--no-cache", "--files-per-call", "--force", "--auto",
    "--show-name",
])
def test_translate_flags_are_documented(flag):
    assert flag in readme(), f"{flag} is undocumented in README.md"


@pytest.mark.parametrize("flag", ["--check", "--branch", "--stash"])
def test_update_flags_are_documented(flag):
    assert flag in readme(), f"{flag} is undocumented in README.md"


@pytest.mark.parametrize("flag", ["--apply", "--keep", "--what"])
def test_prune_flags_are_documented(flag):
    assert flag in readme(), f"{flag} is undocumented in README.md"


def test_readme_flag_examples_are_flags_argparse_accepts():
    """Catches a documented flag that was renamed or never existed.

    Only long flags in `backticks` are checked, and only against the command
    whose help page should list them.
    """
    text = readme()
    helps = {command: run_cli(command, "-h") for command in SUBCOMMANDS}
    helps["global"] = run_cli("-h")

    documented = set(re.findall(r'`(--[a-z][a-z0-9-]+)', text))
    unknown = [flag for flag in documented
               if not any(flag in page for page in helps.values())]
    assert not unknown, f"README documents flags argparse does not accept: {unknown}"


# ── project layout ────────────────────────────────────────────────────────────

def test_readme_module_map_lists_every_module():
    text = readme()
    modules = sorted(p.name for p in (ROOT / "btcli").glob("*.py")
                     if not p.name.startswith("__"))
    missing = [name for name in modules if name not in text]
    assert not missing, f"README's module map omits: {missing}"


def test_readme_module_map_has_no_ghosts():
    """A module listed but deleted sends readers looking for nothing."""
    listed = set(re.findall(r'^├── (\w+\.py)|^└── (\w+\.py)',
                            readme(), re.MULTILINE))
    names = {a or b for a, b in listed}
    actual = {p.name for p in (ROOT / "btcli").glob("*.py")}
    ghosts = sorted(names - actual)
    assert not ghosts, f"README lists modules that do not exist: {ghosts}"
