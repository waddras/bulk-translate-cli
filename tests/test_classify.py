"""Letting Gemini choose the track and styles.

The motivating case is a release with forty styles — sign1..sign10, NodameOP,
EdEnglish, letter1, gyabo — where picking by hand is guesswork. Two things have
to hold for that to be safe: the model must be given evidence strong enough to
judge on (cue counts, not names), and nothing it says may be taken on trust.

The validation tests are the important ones. A model that invents a style name,
names a track that was never offered, or answers in the wrong shape must never
widen what gets translated — the reply is discarded and the user is asked
instead.
"""
from __future__ import annotations

import json

import pytest

from btcli import classify


@pytest.fixture
def sample(tmp_path, ass_factory):
    """A file shaped like a real release: one busy dialogue style, plus noise."""
    cues = [("Dialogue", f"line {index}") for index in range(30)]
    cues += [("Sign", "TOKYO"), ("Sign", "3 YEARS EARLIER")]
    cues += [("OP", "la la la")]
    return ass_factory(tmp_path / "e01.en.ass", cues,
                       styles=("Dialogue", "Sign", "OP"))


# ── Evidence ──────────────────────────────────────────────────────────────────

def test_style_facts_count_cues_and_order_by_volume(sample):
    """Cue count is the strongest signal, so it leads."""
    facts = classify.style_facts(sample)
    by_name = {fact["name"]: fact for fact in facts}

    assert by_name["Dialogue"]["cues"] == 30
    assert by_name["Sign"]["cues"] == 2
    assert [fact["name"] for fact in facts][0] == "Dialogue", "busiest style first"


def test_style_facts_carry_a_few_samples_only(sample):
    facts = {fact["name"]: fact for fact in classify.style_facts(sample)}
    assert len(facts["Dialogue"]["samples"]) == classify.SAMPLE_LINES_PER_STYLE
    assert facts["Sign"]["samples"] == ["TOKYO", "3 YEARS EARLIER"]


def test_positioned_and_karaoke_cues_are_flagged(tmp_path, ass_factory):
    """\\pos means a sign, \\k means an opening or ending. Both are strong hints."""
    path = ass_factory(tmp_path / "a.ass",
                       [("Sign", r"{\pos(120,400)}SHOP"),
                        ("OP", r"{\k25}la"),
                        ("Dialogue", "Good morning")],
                       styles=("Sign", "OP", "Dialogue"))
    facts = {fact["name"]: fact for fact in classify.style_facts(path)}

    assert facts["Sign"]["positioned"] is True
    assert facts["OP"]["karaoke"] is True
    assert facts["Dialogue"]["positioned"] is False
    assert facts["Dialogue"]["karaoke"] is False


def test_an_unreadable_file_yields_no_facts(tmp_path):
    broken = tmp_path / "broken.ass"
    broken.write_text("this is not a subtitle file", encoding="utf-8")
    assert classify.style_facts(broken) == []


def test_samples_are_trimmed(tmp_path, ass_factory):
    path = ass_factory(tmp_path / "a.ass", [("D", "x" * 500)], styles=("D",))
    sample_line = classify.style_facts(path)[0]["samples"][0]
    assert len(sample_line) == classify.MAX_SAMPLE_CHARS


# ── The prompt ────────────────────────────────────────────────────────────────

def _candidates():
    return [{
        "index": 0, "codec": "ass", "language": "eng", "title": "Full Subtitles",
        "styles": [
            {"name": "Dialogue", "cues": 300, "positioned": False,
             "karaoke": False, "samples": ["Good morning"]},
            {"name": "Sign", "cues": 3, "positioned": True,
             "karaoke": False, "samples": ["SHOP"]},
        ],
    }]


def test_the_prompt_carries_the_evidence_and_the_contract():
    prompt = classify._build_prompt(_candidates(), "pick the dialogue", "english")

    assert "pick the dialogue" in prompt
    assert "BTCLI OUTPUT CONTRACT" in prompt
    assert "overrides any earlier output-format wording" in prompt
    assert '"cues": 300' in prompt, "cue counts must reach the model"
    assert "Full Subtitles" in prompt, "track titles say plainly what a track is"


def test_the_contract_asks_for_a_track_only_when_there_is_a_choice():
    """Subtitle files have no track, so asking for one invites an invented value."""
    assert "Set track to" in classify._output_contract(multi_track=True)
    assert "Omit track" in classify._output_contract(multi_track=False)


def test_the_verdict_schema_is_not_the_translation_schema():
    """ai._response_schema pins replies to [{id,text}], which would break this."""
    from btcli.ai import _response_schema

    assert classify._verdict_schema()["type"] == "OBJECT"
    assert _response_schema()["type"] == "ARRAY"
    assert classify._generation_config()["responseSchema"] == classify._verdict_schema()


# ── Validation: nothing is taken on trust ─────────────────────────────────────

def test_a_good_reply_becomes_a_verdict():
    verdict = classify.validate(
        {"track": 0, "dialogue_styles": ["Dialogue"], "reason": "hundreds of cues"},
        _candidates())

    assert verdict["track"] == 0
    assert verdict["keep"] == ["Dialogue"]
    assert verdict["passthrough"] == ["+ALL"], "the rest passes through untouched"
    assert verdict["reason"] == "hundreds of cues"
    assert verdict["styles"] == ["Dialogue", "Sign"], "recorded for cache checking"


def test_an_invented_style_name_is_dropped():
    verdict = classify.validate(
        {"track": 0, "dialogue_styles": ["Dialogue", "MainDialogue"]}, _candidates())
    assert verdict["keep"] == ["Dialogue"], "only styles the file really has"


def test_a_reply_of_only_invented_styles_is_refused():
    """Refusing sends the user to the normal prompt. Widening would cost quota."""
    assert classify.validate(
        {"track": 0, "dialogue_styles": ["Nope", "AlsoNope"]}, _candidates()) is None


def test_an_unknown_track_is_refused():
    assert classify.validate(
        {"track": 7, "dialogue_styles": ["Dialogue"]}, _candidates()) is None


def test_an_empty_selection_is_refused():
    assert classify.validate({"track": 0, "dialogue_styles": []}, _candidates()) is None


def test_a_reply_that_is_not_an_object_is_refused():
    for reply in ("Dialogue", 3, None, [], {}, {"styles": ["Dialogue"]}):
        assert classify.validate(reply, _candidates()) is None, reply


def test_a_case_slip_is_tolerated_but_a_different_name_is_not():
    """Echoing "dialogue" for "Dialogue" is a formatting slip, not an invention."""
    verdict = classify.validate(
        {"track": 0, "dialogue_styles": ["dialogue"]}, _candidates())
    assert verdict["keep"] == ["Dialogue"], "matched back to the real name"


def test_a_wrapped_or_listed_verdict_is_still_read():
    """Models asked for one object sometimes send [obj] or {"verdict": obj}."""
    good = {"track": 0, "dialogue_styles": ["Dialogue"]}
    assert classify.validate([good], _candidates())["keep"] == ["Dialogue"]
    assert classify.validate({"verdict": good}, _candidates())["keep"] == ["Dialogue"]


def test_duplicates_are_collapsed():
    verdict = classify.validate(
        {"track": 0, "dialogue_styles": ["Dialogue", "Dialogue", "dialogue"]},
        _candidates())
    assert verdict["keep"] == ["Dialogue"]


def test_subtitle_files_ignore_a_volunteered_track():
    """There is no track to choose, so one offered anyway is noise, not an error."""
    candidates = [{"index": None, "styles": _candidates()[0]["styles"]}]
    verdict = classify.validate(
        {"track": 4, "dialogue_styles": ["Dialogue"]}, candidates)
    assert verdict is not None
    assert verdict["track"] is None


# ── The call ──────────────────────────────────────────────────────────────────

def test_choose_returns_none_without_a_key():
    assert classify.choose(_candidates(), "") is None


def test_choose_returns_none_without_styles():
    assert classify.choose([{"index": 0, "styles": []}], "key") is None


def test_choose_uses_the_pinned_model_and_never_paces(monkeypatch, isolated_settings):
    """Pacing would stall the questionnaire; the ladder would spend other quota."""
    isolated_settings["AI_SELECT_MODEL"] = "pinned-model"
    seen = {}

    async def fake_call(client, prompt, api_key, model=None, attempt=1,
                        gen_config=None):
        seen["model"] = model
        seen["gen_config"] = gen_config
        return {"track": 0, "dialogue_styles": ["Dialogue"]}

    def explode():
        raise AssertionError("a selection call must not pace")

    monkeypatch.setattr("btcli.ai._call_gemini", fake_call)
    monkeypatch.setattr("btcli.ai.pace_requests", explode)

    verdict = classify.choose(_candidates(), "key")
    assert verdict["keep"] == ["Dialogue"]
    assert seen["model"] == "pinned-model"
    assert seen["gen_config"]["responseSchema"]["type"] == "OBJECT", \
        "must not inherit the translation array schema"


def test_choose_survives_a_refused_call(monkeypatch):
    async def refuse(*args, **kwargs):
        return None

    monkeypatch.setattr("btcli.ai._call_gemini", refuse)
    assert classify.choose(_candidates(), "key") is None


def test_choose_survives_a_raising_call(monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("network gone")

    monkeypatch.setattr("btcli.ai._call_gemini", explode)
    assert classify.choose(_candidates(), "key") is None


def test_describe_reads_as_a_style_selection():
    assert classify.describe({"keep": ["A", "B"]}) == "A,B,+ALL"


# ── Caching the verdict ───────────────────────────────────────────────────────

def test_a_verdict_is_saved_and_reloaded(tmp_path):
    from btcli.manifest import load_style_verdict, save_style_verdict

    verdict = {"track": 0, "keep": ["Dialogue"], "passthrough": ["+ALL"],
               "styles": ["Dialogue", "Sign"], "reason": "why", "model": "m"}
    save_style_verdict(tmp_path, verdict)

    loaded = load_style_verdict(tmp_path, ["Dialogue", "Sign"])
    assert loaded["keep"] == ["Dialogue"]
    assert loaded["track"] == 0
    assert loaded["decided_at"], "recorded so a stale verdict is visible"


def test_a_verdict_is_ignored_when_the_styles_changed(tmp_path):
    """A re-release with renamed styles must be re-judged, not guessed at."""
    from btcli.manifest import load_style_verdict, save_style_verdict

    save_style_verdict(tmp_path, {"track": 0, "keep": ["Dialogue"],
                                  "passthrough": ["+ALL"],
                                  "styles": ["Dialogue", "Sign"]})
    assert load_style_verdict(tmp_path, ["Base01", "sign1"]) is None


def test_a_verdict_is_ignored_when_a_chosen_style_is_gone(tmp_path):
    from btcli.manifest import load_style_verdict, save_style_verdict

    save_style_verdict(tmp_path, {"track": 0, "keep": ["Dialogue"],
                                  "passthrough": ["+ALL"],
                                  "styles": ["Dialogue"]})
    assert load_style_verdict(tmp_path, ["Sign"]) is None


def test_no_verdict_is_not_an_error(tmp_path):
    from btcli.manifest import load_style_verdict

    assert load_style_verdict(tmp_path, ["Dialogue"]) is None


def test_saving_a_verdict_keeps_the_job_history(tmp_path):
    """Jobs are append-only history; a verdict is current state beside them."""
    from btcli.manifest import MANIFEST_NAME, save_style_verdict

    (tmp_path / MANIFEST_NAME).write_text(
        json.dumps({"series": "Show", "season": "S01",
                    "jobs": {"job1": {"command": {}}}}), encoding="utf-8")

    save_style_verdict(tmp_path, {"track": 0, "keep": ["A"], "passthrough": ["+ALL"],
                                  "styles": ["A"]})

    data = json.loads((tmp_path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert "job1" in data["jobs"], "history must survive"
    assert data["series"] == "Show"
    assert data["ai_verdict"]["keep"] == ["A"]
