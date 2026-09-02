"""Tests for the update flow.

Each test builds a real throwaway git repository with a local "remote", so the
update logic is exercised against actual git behaviour rather than mocks. The
cases mirror the ways a user's install drifts in practice: local edits, local
commits, a detached HEAD, a branch that only exists on the remote.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from btcli import update as update_module
from btcli.update import (
    UpdateError,
    current_branch,
    local_changes,
    merge_settings,
    run_update,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=str(repo),
                            capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def repo_pair(tmp_path):
    """An origin repo plus a clone of it, both with one commit on main.

    Returns (clone, origin). The clone is what stands in for the user's
    /opt/btcli install.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--quiet", "--initial-branch=main")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")

    (origin / "btcli").mkdir()
    (origin / "btcli" / "main.py").write_text("# v1\n", encoding="utf-8")
    (origin / "settings.default.conf").write_text(
        '{\n  // comment\n  "TARGET_LANGUAGE": "arabic"\n}\n', encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "--quiet", "-m", "initial")

    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(origin), str(clone))
    git(clone, "config", "user.email", "test@example.com")
    git(clone, "config", "user.name", "Test")
    return clone, origin


@pytest.fixture
def install(repo_pair, monkeypatch):
    """Point the update flow at the throwaway clone."""
    clone, origin = repo_pair
    monkeypatch.setattr(update_module, "find_repo", lambda: clone)
    return clone, origin


def commit_to_origin(origin, text="# v2\n", message="second"):
    (origin / "btcli" / "main.py").write_text(text, encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "--quiet", "-m", message)


def head(repo):
    return git(repo, "rev-parse", "HEAD")


def output_of(capsys):
    """All captured output as one whitespace-normalised string.

    The logger wraps long lines to the terminal width, so a phrase under test
    can be split across lines. Collapsing whitespace keeps these assertions
    about the message rather than about the wrapping.
    """
    captured = capsys.readouterr()
    return " ".join((captured.out + " " + captured.err).split())


# ── repo inspection ───────────────────────────────────────────────────────────

def test_find_repo_rejects_a_non_checkout(tmp_path, monkeypatch):
    """A zip-file install has no .git, and should be told so plainly."""
    monkeypatch.setattr(update_module, "_INSTALL_DIR", tmp_path / "nope")
    monkeypatch.setattr(update_module.Path, "resolve",
                        lambda self: tmp_path / "pkg" / "update.py")
    with pytest.raises(UpdateError, match="not a git checkout"):
        update_module.find_repo()


def test_current_branch_reports_the_branch(install):
    clone, _ = install
    assert current_branch(clone) == "main"


def test_current_branch_is_none_when_detached(install):
    clone, _ = install
    git(clone, "checkout", "--quiet", "--detach", "HEAD")
    assert current_branch(clone) is None


def test_local_changes_lists_modified_tracked_files(install):
    clone, _ = install
    (clone / "btcli" / "main.py").write_text("# edited\n", encoding="utf-8")
    assert local_changes(clone) == ["btcli/main.py"]


def test_local_changes_ignores_untracked_files(install):
    clone, _ = install
    (clone / "settings.conf").write_text("{}\n", encoding="utf-8")
    assert local_changes(clone) == []


# ── the happy path ────────────────────────────────────────────────────────────

def test_update_fast_forwards_to_the_remote(install):
    clone, origin = install
    commit_to_origin(origin)

    run_update()

    assert head(clone) == head(origin)
    assert (clone / "btcli" / "main.py").read_text() == "# v2\n"


def test_update_is_a_no_op_when_already_current(install):
    clone, _ = install
    before = head(clone)
    run_update()
    assert head(clone) == before


def test_update_creates_settings_conf_on_first_run(install):
    clone, _ = install
    assert not (clone / "settings.conf").exists()

    run_update()

    created = (clone / "settings.conf").read_text(encoding="utf-8")
    assert "TARGET_LANGUAGE" in created
    assert "// comment" in created, "comments from the defaults should survive"


# ── refusing to do damage ─────────────────────────────────────────────────────

def test_update_refuses_when_tracked_files_are_edited(install, capsys):
    clone, origin = install
    commit_to_origin(origin)
    (clone / "btcli" / "main.py").write_text("# my edit\n", encoding="utf-8")
    before = head(clone)

    run_update()

    assert head(clone) == before, "must not move HEAD over local edits"
    assert (clone / "btcli" / "main.py").read_text() == "# my edit\n"
    assert "--stash" in output_of(capsys)


def test_update_refuses_when_the_checkout_has_local_commits(install, capsys):
    clone, origin = install
    commit_to_origin(origin)
    (clone / "mine.txt").write_text("local work\n", encoding="utf-8")
    git(clone, "add", "-A")
    git(clone, "commit", "--quiet", "-m", "my own commit")
    before = head(clone)

    run_update()

    assert head(clone) == before, "fast-forward only: local commits are kept"
    assert "rebase" in output_of(capsys)


def test_update_refuses_on_a_detached_head(install, capsys):
    clone, _ = install
    git(clone, "checkout", "--quiet", "--detach", "HEAD")

    run_update()

    assert "detached" in output_of(capsys)


def test_update_reports_a_missing_remote_branch_and_the_alternatives(install, capsys):
    clone, _ = install
    before = head(clone)

    run_update(branch="does-not-exist")

    output = output_of(capsys)
    assert "no branch named 'does-not-exist'" in output
    assert "main" in output, "should say which branches do exist"
    assert head(clone) == before


def test_update_works_in_a_single_branch_clone(tmp_path, monkeypatch, repo_pair):
    """A --single-branch clone tracks one branch, so a plain fetch is not enough.

    This is how the sandbox clone is set up, and how anyone using --depth 1
    ends up. The explicit refspec has to create the missing tracking ref.
    """
    _, origin = repo_pair
    git(origin, "checkout", "--quiet", "-b", "feature")
    (origin / "feature.txt").write_text("feature work\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "--quiet", "-m", "feature work")
    git(origin, "checkout", "--quiet", "main")

    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "--quiet", "--single-branch", "--branch", "main",
        str(origin), str(shallow))
    git(shallow, "config", "user.email", "test@example.com")
    git(shallow, "config", "user.name", "Test")
    assert "origin/feature" not in git(shallow, "branch", "--remotes")

    monkeypatch.setattr(update_module, "find_repo", lambda: shallow)
    run_update(branch="feature")

    assert current_branch(shallow) == "feature"
    assert (shallow / "feature.txt").exists()


def test_single_branch_clone_is_widened_so_plain_git_works_again(tmp_path, repo_pair):
    """After widening, the user's own `git pull` works on the new branch too."""
    _, origin = repo_pair
    shallow = tmp_path / "narrow"
    git(tmp_path, "clone", "--quiet", "--single-branch", "--branch", "main",
        str(origin), str(shallow))
    assert git(shallow, "config", "--get-all", "remote.origin.fetch") \
        != update_module.STANDARD_REFSPEC

    assert update_module.widen_fetch_refspec(shallow) is True
    assert git(shallow, "config", "--get-all", "remote.origin.fetch") \
        == update_module.STANDARD_REFSPEC

    assert update_module.widen_fetch_refspec(shallow) is False, \
        "already widened, so nothing to change"


def test_widening_leaves_a_normal_clone_alone(install):
    clone, _ = install
    assert update_module.widen_fetch_refspec(clone) is False


# ── --stash ───────────────────────────────────────────────────────────────────

def test_stash_updates_then_restores_local_edits(install):
    clone, origin = install
    (origin / "other.txt").write_text("new file\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "--quiet", "-m", "unrelated change")
    (clone / "btcli" / "main.py").write_text("# my edit\n", encoding="utf-8")

    run_update(stash=True)

    assert head(clone) == head(origin), "the update went through"
    assert (clone / "other.txt").exists(), "new upstream file arrived"
    assert (clone / "btcli" / "main.py").read_text() == "# my edit\n", \
        "the local edit came back"


def test_stash_keeps_edits_recoverable_when_restore_conflicts(install, capsys):
    """Upstream touched the same file the user edited.

    The pop conflicts, so btcli must say where the work went rather than
    silently losing it.
    """
    clone, origin = install
    commit_to_origin(origin, "# upstream change\n")
    (clone / "btcli" / "main.py").write_text("# my edit\n", encoding="utf-8")

    run_update(stash=True)

    assert git(clone, "stash", "list") != "", "the edit is still stashed"
    assert "git stash list" in output_of(capsys)


# ── --branch ──────────────────────────────────────────────────────────────────

def test_branch_switches_and_updates(install):
    clone, origin = install
    git(origin, "checkout", "--quiet", "-b", "release")
    (origin / "release.txt").write_text("release only\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "--quiet", "-m", "release work")

    run_update(branch="release")

    assert current_branch(clone) == "release"
    assert (clone / "release.txt").exists()


# ── --check ───────────────────────────────────────────────────────────────────

def test_check_reports_available_commits_without_applying_them(install, capsys):
    clone, origin = install
    commit_to_origin(origin, message="a shiny new feature")
    before = head(clone)

    run_update(check=True)

    assert head(clone) == before, "--check must not move HEAD"
    assert "a shiny new feature" in output_of(capsys)


def test_check_does_not_create_settings_conf(install):
    clone, _ = install
    run_update(check=True)
    assert not (clone / "settings.conf").exists()


def test_check_says_up_to_date_when_there_is_nothing_new(install, capsys):
    run_update(check=True)
    assert "Up to date" in output_of(capsys)


# ── settings merge ────────────────────────────────────────────────────────────

def write_conf(path, body):
    path.write_text(body, encoding="utf-8")
    return path


def test_merge_adds_missing_keys_and_keeps_user_values(tmp_path):
    default = write_conf(tmp_path / "d.conf",
                         '{"A": 1, "B": 2, "C": 3}')
    user = write_conf(tmp_path / "u.conf",
                      '{\n  // my notes\n  "A": 99\n}\n')

    summary = merge_settings(user, default)

    assert sorted(summary["added"]) == ["B", "C"]
    merged = json.loads(
        update_module.re.sub(r'(?m)^\s*//.*$', '', user.read_text()))
    assert merged == {"A": 99, "B": 2, "C": 3}, "user value for A survived"
    assert "// my notes" in user.read_text(), "comments survived"


def test_merge_reports_deprecated_keys_without_deleting_them(tmp_path):
    default = write_conf(tmp_path / "d.conf", '{"A": 1}')
    user = write_conf(tmp_path / "u.conf", '{"A": 1, "OLD_KEY": true}')

    summary = merge_settings(user, default)

    assert summary["deprecated"] == ["OLD_KEY"]
    assert "OLD_KEY" in user.read_text(), "deprecated keys are only reported"


def test_merge_handles_an_empty_user_config(tmp_path):
    default = write_conf(tmp_path / "d.conf", '{"A": 1}')
    user = write_conf(tmp_path / "u.conf", '{\n}\n')

    merge_settings(user, default)

    assert json.loads(user.read_text()) == {"A": 1}


def test_merge_preview_does_not_write(tmp_path):
    default = write_conf(tmp_path / "d.conf", '{"A": 1, "B": 2}')
    user = write_conf(tmp_path / "u.conf", '{"A": 1}')

    summary = merge_settings(user, default, apply=False)

    assert summary["added"] == {"B": 2}
    assert json.loads(user.read_text()) == {"A": 1}, "nothing was written"


def test_merge_rejects_an_unparseable_user_config(tmp_path):
    default = write_conf(tmp_path / "d.conf", '{"A": 1}')
    user = write_conf(tmp_path / "u.conf", '{"A": oops}')

    with pytest.raises(UpdateError, match="Could not parse"):
        merge_settings(user, default)


def test_merge_survives_trailing_commas_in_the_user_config(tmp_path):
    default = write_conf(tmp_path / "d.conf", '{"A": 1, "B": 2}')
    user = write_conf(tmp_path / "u.conf", '{\n  "A": 1,\n}\n')

    merge_settings(user, default)

    assert json.loads(user.read_text()) == {"A": 1, "B": 2}


def test_merge_writes_valid_json_for_list_and_dict_values(tmp_path):
    default = write_conf(
        tmp_path / "d.conf",
        json.dumps({"POOL": ["a", "b"], "NESTED": {"x": 1}, "TEXT": "hi"}))
    user = write_conf(tmp_path / "u.conf", '{"KEEP": 1}')

    merge_settings(user, default)

    assert json.loads(user.read_text()) == {
        "KEEP": 1, "POOL": ["a", "b"], "NESTED": {"x": 1}, "TEXT": "hi"}


def test_merge_needs_the_defaults_file(tmp_path):
    with pytest.raises(UpdateError, match="not found"):
        merge_settings(tmp_path / "u.conf", tmp_path / "missing.conf")
