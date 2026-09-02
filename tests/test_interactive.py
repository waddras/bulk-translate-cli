"""Interactive mode: style syntax, folder picker, back navigation.

Covers the style DSL used to skip karaoke, the folder picker used to skip whole
seasons, and the step machine that lets any prompt go back.
"""
from __future__ import annotations

import builtins

import pytest

from btcli import interactive, prompts
from btcli.interactive import _resolve_folder_selection, _resolve_style_tokens
from btcli.prompts import BACK
from btcli.styles import parse_styles_arg, resolve_styles_with_files

STYLES = ["Default", "Default-Alt", "Signs", "OP-EN", "ED-EN"]


@pytest.fixture
def answers(monkeypatch):
    """Feed scripted answers to input(), and pretend we have a terminal."""
    def feed(sequence):
        iterator = iter(sequence)
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(iterator))
        monkeypatch.setattr(prompts.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(prompts.sys.stdout, "isatty", lambda: False, raising=False)
    return feed


# ── Style selection ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("+ALL,1", ["+ALL", "Default"]),
    ("1,2,+karaoke", ["Default", "Default-Alt", "+karaoke"]),
    ("ALL,+karaoke", ["ALL", "+karaoke"]),
    ("1,3,+ALL", ["Default", "Signs", "+ALL"]),
    ("+3", ["+Signs"]),
    ("1,+4,+5", ["Default", "+OP-EN", "+ED-EN"]),
    ("Default,+Signs", ["Default", "+Signs"]),
    ("all,+all", ["ALL", "+ALL"]),
    (" 1 , +3 ", ["Default", "+Signs"]),
    ("3,1", ["Signs", "Default"]),
])
def test_style_tokens_resolve(raw, expected):
    assert _resolve_style_tokens(raw, STYLES) == expected


@pytest.mark.parametrize("raw", ["9", "+0", "Nope", "", "0"])
def test_bad_style_input_is_rejected(raw):
    with pytest.raises(ValueError):
        _resolve_style_tokens(raw, STYLES)


def test_plus_all_with_one_number_translates_only_that_style(tmp_path, ass_factory):
    """'+ALL,1' is the documented way to translate one style and pass the rest."""
    sample = ass_factory(tmp_path / "s.ass",
                         [(name, "text") for name in STYLES], styles=tuple(STYLES))
    tokens = _resolve_style_tokens("+ALL,1", STYLES)
    keep, passthrough = parse_styles_arg(",".join(tokens))
    keep, passthrough = resolve_styles_with_files(keep, passthrough, STYLES, [str(sample)])

    assert keep == ["Default"]
    assert set(passthrough) == set(STYLES) - {"Default"}


# ── Folder picker ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,count,expected", [
    ("ALL", 4, [1, 2, 3, 4]),
    ("all", 4, [1, 2, 3, 4]),
    ("1,3", 4, [1, 3]),
    ("-3", 4, [1, 2, 4]),
    ("-1,-2", 4, [3, 4]),
    ("3,1", 4, [1, 3]),
    ("2,2", 4, [2]),
])
def test_folder_selection_resolves(raw, count, expected):
    assert _resolve_folder_selection(raw, count) == expected


@pytest.mark.parametrize("raw", ["5", "0", "1,-2", "ALL,1", "x", "", "-1,-2,-3,-4"])
def test_bad_folder_selection_is_rejected(raw):
    with pytest.raises(ValueError):
        _resolve_folder_selection(raw, 4)


# ── Back navigation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("word", ["b", "B", "back", " back "])
def test_every_prompt_helper_can_go_back(word, monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt="": word)
    assert prompts.ask("q", "d", allow_back=True) is BACK
    assert prompts.ask_yes_no("q", True, allow_back=True) is BACK
    assert prompts.ask_choice("q", ["vid", "sub"], "vid", allow_back=True) is BACK
    assert prompts.ask_optional_int("q", "auto", allow_back=True) is BACK
    assert prompts.ask_menu("q", [("y", "yes")], "y", allow_back=True) is BACK


def test_b_is_ordinary_input_when_back_is_not_offered(monkeypatch):
    """A folder or style genuinely named 'b' must still be usable."""
    monkeypatch.setattr(builtins, "input", lambda prompt="": "b")
    assert prompts.ask("q", "d") == "b"


def test_defaults_still_apply_with_back_enabled(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")
    assert prompts.ask_yes_no("q", True, allow_back=True) is True
    assert prompts.ask_optional_int("q", "auto", allow_back=True) is None


def test_full_flow_with_back_at_every_step(series, answers, monkeypatch):
    """Walk forward, go back at each stage, then finish. Nothing should be lost."""
    calls = []
    monkeypatch.setattr(
        "btcli.translate.run_translate",
        lambda **kw: (calls.append(kw), {"missing": {}, "path": kw["path"]})[1])

    answers([
        "sub", str(series),
        "b",                 # back to input type
        "sub", str(series),
        "ALL",
        "b",                 # back from the folder picker to the path
        str(series),
        "1,2",
        "1",                 # season 1 styles
        "b",                 # back at season 2 styles -> redo season 1
        "2",
        "1",                 # season 2 styles
        "n",                 # force
        "b",                 # back from files-per-call to force
        "n",
        "",                  # files-per-call = auto
        "b",                 # back from the summary
        "",
        "y",                 # proceed
    ])
    interactive.run_interactive(path=None)
    assert len(calls) == 2, "both seasons should run after all that stepping"


def test_summary_can_edit_and_drop_folders(series, answers, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "btcli.translate.run_translate",
        lambda **kw: (calls.append(kw), {"missing": {}, "path": kw["path"]})[1])

    answers([
        "sub", str(series), "1,2",
        "1",            # season 1 styles
        "1",            # season 2 styles
        "n", "",        # force, files-per-call
        "e", "1", "2",  # edit folder 1, choose different styles
        "d", "2",       # drop folder 2
        "y",            # proceed
    ])
    interactive.run_interactive(path=None)

    assert len(calls) == 1, "one folder was dropped"
    assert calls[0]["keep_styles"] == ["Signs"], "edited style should be used"


def test_interactive_refuses_without_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(prompts.sys.stdin, "isatty", lambda: False, raising=False)
    interactive.run_interactive(path=None)


def test_cancelling_the_summary_writes_nothing(series, answers, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "btcli.translate.run_translate",
        lambda **kw: calls.append(kw))
    answers(["sub", str(series), "1,2", "1", "1", "n", "", "n"])
    interactive.run_interactive(path=None)
    assert calls == []
