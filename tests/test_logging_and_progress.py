"""Logging and progress reporting.

Covers the duplicated log lines in verbose mode, rich markup leaking into log
files, and the progress bar that froze at 10/10 because failed chunks counted as
progress and completed the task while retries were still running.
"""
from __future__ import annotations

import os

from btcli import prompts
from btcli.logger import Logger


# ── Log file hygiene ──────────────────────────────────────────────────────────

def _log_lines(path):
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_detail_is_written_once_in_verbose_mode(tmp_path):
    """detail() wrote to the file and then printed, duplicating every line."""
    path = tmp_path / "run.log"
    log = Logger()
    log.set_level("full")
    log.set_log_file(str(path))
    log.detail("only once")
    log.close()
    assert sum("only once" in line for line in _log_lines(path)) == 1


def test_attempt_is_written_once_in_verbose_mode(tmp_path):
    path = tmp_path / "run.log"
    log = Logger()
    log.set_level("full")
    log.set_log_file(str(path))
    log.attempt(1, 5, "failed")
    log.close()
    assert sum("Attempt 1/5" in line for line in _log_lines(path)) == 1


def test_markup_is_stripped_from_the_log_file(tmp_path):
    path = tmp_path / "run.log"
    log = Logger()
    log.set_level("medium")
    log.set_log_file(str(path))
    log.stat("Path", "/media/show")
    log.close()
    lines = _log_lines(path)
    assert not any("[bold]" in line for line in lines)
    assert any("Path: /media/show" in line for line in lines)


def test_bracketed_text_is_not_mistaken_for_markup(tmp_path):
    """[01] and [Fonts] must survive; only style tags are stripped."""
    path = tmp_path / "run.log"
    log = Logger()
    log.set_level("medium")
    log.set_log_file(str(path))
    log.item("[01] episode.mkv")
    log.detail("[Fonts] section found")
    log.close()
    text = "\n".join(_log_lines(path))
    assert "[01] episode.mkv" in text
    assert "[Fonts] section found" in text


def test_every_level_writes_to_the_log_file(tmp_path):
    for level in ("minimal", "medium", "full"):
        path = tmp_path / f"{level}.log"
        log = Logger()
        log.set_level(level)
        log.set_log_file(str(path))
        log.detail("recorded")
        log.close()
        assert any("recorded" in line for line in _log_lines(path)), level


# ── Progress bar ──────────────────────────────────────────────────────────────

def test_failed_chunks_do_not_complete_the_bar():
    """A completed task stops rich's clock, which read as a frozen display."""
    log = Logger()
    log.set_level("medium")
    log.start_progress("Translating", total=10)
    for _ in range(7):
        log.advance_progress()          # three chunks failed, so no advance
    task = log._progress.tasks[0]
    assert task.completed == 7
    assert not task.finished, "bar must not finish while lines are missing"
    log.finish_progress()


def test_starting_a_second_phase_stops_the_first():
    """Rich allows only one live display, so the first bar must be torn down.

    Asserting on the new bar alone is not enough: the bug is the OLD display
    being left running, which is what duplicated the frozen bar on every log
    line during retries.
    """
    log = Logger()
    log.set_level("medium")
    log.start_progress("Translating", total=3)
    first = log._progress
    assert first.live.is_started

    log.start_progress("Retry round 1/5", total=2)
    assert not first.live.is_started, "the previous live display was left running"
    assert log._progress is not first
    assert log._progress.tasks[0].description.startswith("Retry")
    log.finish_progress()


def test_finishing_twice_is_harmless():
    log = Logger()
    log.set_level("medium")
    log.start_progress("x", total=1)
    log.finish_progress()
    log.finish_progress()
    assert log._progress is None


# ── Prompt colour ─────────────────────────────────────────────────────────────

def test_colour_is_applied_on_a_terminal(monkeypatch):
    monkeypatch.setattr(prompts.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert prompts.paint("hi", prompts.QUESTION).startswith("\033[")


def test_no_colour_env_disables_colour(monkeypatch):
    monkeypatch.setattr(prompts.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert prompts.paint("hi", prompts.QUESTION) == "hi"


def test_dumb_terminal_disables_colour(monkeypatch):
    monkeypatch.setattr(prompts.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert prompts.paint("hi", prompts.QUESTION) == "hi"


def test_redirected_output_stays_plain(monkeypatch):
    monkeypatch.setattr(prompts.sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert prompts.paint("hi", prompts.HEADING) == "hi"


def test_columns_align_regardless_of_colour(monkeypatch, capsys):
    """Padding must be measured on visible text, not escape codes."""
    monkeypatch.setattr(prompts.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    prompts.columns(["1) Default", "2) Signs", "3) OP-EN"], per_row=3, width=20)
    plain = capsys.readouterr().out
    for code in ("\033[96m", "\033[0m"):
        plain = plain.replace(code, "")
    assert plain.index("2)") == 2 + 20
