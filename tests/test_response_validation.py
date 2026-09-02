"""Model response validation.

Covers the bug that corrupted real output: translations arriving attached to the
wrong cue, which produced subtitles where one line showed another line's text.
Also covers the follow-up bug where a correct array-shaped reply was rejected
outright, wasting every retry.
"""
from __future__ import annotations

from btcli.ai import (
    _generation_config,
    _normalize_result,
    _wire_items,
    effective_attempts,
    model_ladder,
)

SOURCE = {
    "010001": "Hello jake\nmy name is kamal",
    "010002": "Bye.",
}


def _reply(pairs):
    return [{"id": key, "text": f"<BTCLI_ID:{key}> {value}"} for key, value in pairs]


def test_wire_payload_is_an_array_with_protected_breaks():
    items = _wire_items(SOURCE)
    assert isinstance(items, list)
    assert items[0]["id"] == "010001"
    assert "<BTCLI_LB>" in items[0]["text"], "line breaks must be protected"


def test_array_reply_is_accepted():
    """A bare JSON array used to be discarded with 'JSON root is not an object'."""
    result = _normalize_result(
        _reply([("010001", "مرحبا<BTCLI_LB>كمال"), ("010002", "وداعا")]), SOURCE)
    assert result["010001"] == "مرحبا\nكمال"
    assert result["010002"] == "وداعا"


def test_wrapped_array_reply_is_accepted():
    payload = {"translations": _reply([("010001", "أ"), ("010002", "ب")])}
    assert set(_normalize_result(payload, SOURCE)) == {"010001", "010002"}


def test_keyed_object_reply_is_accepted():
    payload = {item["id"]: item for item in _reply([("010001", "أ"), ("010002", "ب")])}
    assert set(_normalize_result(payload, SOURCE)) == {"010001", "010002"}


def test_translation_under_the_wrong_key_is_rejected():
    """The line-shift bug: text arriving under a key that is not its own."""
    payload = {"010001": {"id": "010002", "text": "<BTCLI_ID:010002> wrong"}}
    assert _normalize_result(payload, SOURCE) == {}


def test_inline_token_must_match_the_id_field():
    payload = [{"id": "010001", "text": "<BTCLI_ID:010002> mismatched"}]
    assert _normalize_result(payload, SOURCE) == {}


def test_duplicate_ids_are_all_rejected():
    """Three copies of one id must not leave a survivor."""
    payload = _reply([("010001", "a"), ("010001", "b"), ("010001", "c")])
    assert _normalize_result(payload, SOURCE) == {}


def test_unknown_ids_are_ignored_but_valid_ones_kept():
    payload = _reply([("999999", "stray"), ("010002", "وداعا")])
    assert _normalize_result(payload, SOURCE) == {"010002": "وداعا"}


def test_non_json_shapes_are_rejected_without_raising():
    for junk in ("a string", None, 42, [], {}):
        assert _normalize_result(junk, SOURCE) == {}


def test_missing_line_break_is_restored_for_dual_speaker_cues():
    source = {"1": "--Here.\n--Thank you."}
    result = _normalize_result(
        [{"id": "1", "text": "<BTCLI_ID:1> --هنا. --شكرا."}], source)
    assert result["1"].count("\n") == 1, "two speakers must not merge onto one line"


def test_response_schema_pins_the_array_shape():
    schema = _generation_config()["responseSchema"]
    assert schema["type"] == "ARRAY"
    assert schema["items"]["required"] == ["id", "text"]


def test_every_model_is_tried_even_when_retry_attempts_is_low(isolated_settings):
    """A pool larger than RETRY_ATTEMPTS used to leave most models unused."""
    isolated_settings["GEMINI_MODEL"] = "primary"
    isolated_settings["MODEL_POOL"] = ["primary", "second", "third", "fourth"]
    isolated_settings["RETRY_ATTEMPTS"] = 2

    ladder = model_ladder()
    assert ladder == ["primary", "second", "third", "fourth"], "duplicates removed"
    assert effective_attempts() == 4, "attempts raised to cover every model"


def test_pool_repeating_the_primary_does_not_waste_an_attempt(isolated_settings):
    isolated_settings["GEMINI_MODEL"] = "primary"
    isolated_settings["MODEL_POOL"] = ["primary", "primary", "second"]
    assert model_ladder() == ["primary", "second"]
