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
        "sub",
        "b",                 # back from the path to the input type
        "sub", str(series),
        "b",                 # back from the folder picker to the path
        str(series),
        "1,2",
        "b",                 # back from the AI question to the folder picker
        "1,2",
        "n",                 # no, choose the styles by hand
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
        "n",            # no AI selection
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
    answers(["sub", str(series), "1,2", "n", "1", "1", "n", "", "n"])
    interactive.run_interactive(path=None)
    assert calls == []



# ── Letting Gemini choose ─────────────────────────────────────────────────────
#
# The question is asked once for the run; the call happens per folder. Every
# failure path has the same fallback — the ordinary style prompt — because in a
# guided flow the person is sitting right there.


@pytest.fixture
def ai_ready(monkeypatch):
    """Give interactive mode a key, and a stand-in for the selection call.

    Returns the call log. Each canned verdict names the busiest style of
    whichever candidate it was given, so it stays valid for any fixture.
    """
    monkeypatch.setattr("btcli.translate._resolve_api_key", lambda dry_run: "test-key")

    calls = []

    def fake_choose(candidates, api_key, **kwargs):
        calls.append({"candidates": candidates, "instruction": kwargs.get("instruction")})
        available = [fact["name"] for fact in candidates[0]["styles"]]
        return {"track": candidates[0].get("index"), "keep": [available[0]],
                "passthrough": ["+ALL"], "reason": "most cues by far",
                "styles": available, "model": "test-model"}

    monkeypatch.setattr("btcli.classify.choose", fake_choose)
    return calls


@pytest.fixture
def runs(monkeypatch):
    """Capture what run_translate was asked to do."""
    calls = []
    monkeypatch.setattr(
        "btcli.translate.run_translate",
        lambda **kw: (calls.append(kw), {"missing": {}, "path": kw["path"]})[1])
    return calls


def test_an_accepted_verdict_becomes_the_plan(series, answers, ai_ready, runs):
    answers([
        "sub", str(series), "1,2",
        "y", "",         # yes to AI selection, default instruction
        "y",             # accept season 1's verdict
        "y",             # accept season 2's verdict
        "n", "",         # force, files-per-call
        "y",             # proceed
    ])
    interactive.run_interactive(path=None)

    assert len(ai_ready) == 2, "one call per folder, not one per run"
    assert len(runs) == 2
    assert runs[0]["keep_styles"] == ["Default"]
    assert runs[0]["passthrough_styles"] == ["+ALL"], "the rest passes through"


def test_the_default_instruction_is_sent_when_none_is_given(series, answers,
                                                            ai_ready, runs):
    answers(["sub", str(series), "1,2", "y", "", "y", "y", "n", "", "y"])
    interactive.run_interactive(path=None)

    sent = ai_ready[0]["instruction"]
    assert "actual dialogue" in sent
    assert "no openings and no endings" in sent


def test_a_custom_instruction_is_sent_verbatim(series, answers, ai_ready, runs):
    answers([
        "sub", str(series), "1,2",
        "y", "only the main speaking parts",
        "y", "y",
        "n", "", "y",
    ])
    interactive.run_interactive(path=None)
    assert ai_ready[0]["instruction"] == "only the main speaking parts"


def test_declining_a_verdict_falls_back_to_the_style_prompt(series, answers,
                                                            ai_ready, runs):
    answers([
        "sub", str(series), "1,2",
        "y", "",
        "n", "2",        # reject season 1's verdict, pick style 2 by hand
        "n", "2",        # same for season 2
        "n", "", "y",
    ])
    interactive.run_interactive(path=None)

    assert len(runs) == 2
    assert runs[0]["keep_styles"] == ["Signs"], "the hand-picked style wins"


def test_a_failed_call_falls_back_without_asking(series, answers, monkeypatch, runs):
    """A refused call must not cost the user an extra question."""
    monkeypatch.setattr("btcli.translate._resolve_api_key", lambda dry_run: "test-key")
    monkeypatch.setattr("btcli.classify.choose", lambda *a, **k: None)

    answers([
        "sub", str(series), "1,2",
        "y", "",
        "2",             # straight to the style prompt, no confirmation asked
        "2",
        "n", "", "y",
    ])
    interactive.run_interactive(path=None)

    assert len(runs) == 2
    assert runs[0]["keep_styles"] == ["Signs"]


def test_no_api_key_falls_back_without_asking(series, answers, monkeypatch, runs):
    """Reported once, up front, rather than silently per folder."""
    monkeypatch.setattr("btcli.translate._resolve_api_key", lambda dry_run: "")

    answers([
        "sub", str(series), "1,2",
        "y", "",         # asked, but there is no key
        "2", "2",        # so the ordinary prompts run
        "n", "", "y",
    ])
    interactive.run_interactive(path=None)
    assert runs[0]["keep_styles"] == ["Signs"]


def test_saying_no_never_calls_the_api(series, answers, ai_ready, runs):
    answers(["sub", str(series), "1,2", "n", "1", "1", "n", "", "y"])
    interactive.run_interactive(path=None)
    assert ai_ready == [], "declining the offer must not spend a call"


def test_a_cached_verdict_skips_the_call_but_not_the_confirmation(
        series, answers, ai_ready, runs):
    """The cache saves the call, not the decision — nothing stale goes unseen."""
    answers(["sub", str(series), "1,2", "y", "", "y", "y", "n", "", "y"])
    interactive.run_interactive(path=None)
    assert len(ai_ready) == 2
    assert (series / "Season 01" / ".btcli.json").exists()

    # Same folders again, in a second session.
    ai_ready.clear()
    runs.clear()
    answers(["sub", str(series), "1,2", "y", "", "y", "y", "n", "", "y"])
    interactive.run_interactive(path=None)

    assert ai_ready == [], "the verdict was cached, so no call was needed"
    assert len(runs) == 2, "and it still had to be confirmed to be used"
    assert runs[0]["keep_styles"] == ["Default"]



def test_the_numbered_style_list_is_shown_before_the_call(series, answers,
                                                          ai_ready, runs, capsys):
    """The numbers shown must be the numbers sent, or comparing them is useless."""
    answers(["sub", str(series), "1,2", "y", "", "y", "y", "n", "", "y"])
    interactive.run_interactive(path=None)

    output = capsys.readouterr().out
    assert "1) Default" in output
    assert "2) Signs" in output, "season 1's other style, numbered"

    # The same numbering the model was given for that folder.
    from btcli.classify import enumerate_styles
    entries = enumerate_styles(ai_ready[0]["candidates"])
    assert [(entry["number"], entry["name"]) for entry in entries] == \
        [(1, "Default"), (2, "Signs")]


def test_the_verdict_is_labelled_with_those_numbers(series, answers, ai_ready,
                                                    runs, capsys):
    answers(["sub", str(series), "1,2", "y", "", "y", "y", "n", "", "y"])
    interactive.run_interactive(path=None)

    output = capsys.readouterr().out
    assert "translate:   1) Default" in output, \
        "so the choice can be read against the list above it"
