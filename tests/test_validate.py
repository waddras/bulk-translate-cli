"""Settings validation.

settings.conf was trusted without inspection, so a mistake surfaced only as a
confusing runtime failure or as silently degraded behaviour. The motivating case
is covered directly: a pool repeating the primary model plus a low
RETRY_ATTEMPTS meant an entire job reached only two of seven configured models.
"""
from __future__ import annotations

import pytest

from btcli.ai import _build_prompt
from btcli.validate import check_settings, report_settings

SANE = {
    "GEMINI_MODEL": "gemini-3.1-flash-lite",
    "MODEL_POOL": ["gemini-2.5-flash-lite", "gemini-2.5-flash"],
    "RETRY_ATTEMPTS": 5,
    "MAX_LINES_PER_CHUNK": 600,
    "MAX_BLOB_LINES": 50000,
    "PARTIAL_LINE_TOLERANCE": 10,
    "TRANSLATION_MODE": "chunked",
    "FILE_CONFLICT": "overwrite",
    "TARGET_LANGUAGE": "arabic",
    "SOURCE_LANGUAGE": "english",
    "LANGUAGE_CODES": {"arabic": "ar", "english": "en"},
    "SOURCE_EXTENSIONS": [".srt", ".ass"],
}


def test_a_sane_config_is_silent():
    errors, warnings = check_settings(SANE)
    assert errors == []
    assert warnings == []


def test_the_shipped_defaults_are_clean():
    """The real defaults must not trip their own validator."""
    errors, warnings = check_settings()
    assert errors == [], errors
    assert warnings == [], warnings


# ── The configuration that caused the real 429 storm ──────────────────────────

def test_duplicate_models_are_reported():
    conf = dict(SANE, MODEL_POOL=["a", "b", "a"], GEMINI_MODEL="a")
    _, warnings = check_settings(conf)
    assert any("repeats" in w for w in warnings)


def test_retry_attempts_below_the_model_count_is_reported():
    conf = dict(SANE, RETRY_ATTEMPTS=2,
                MODEL_POOL=["a", "b", "c", "d", "e", "f"], GEMINI_MODEL="a")
    _, warnings = check_settings(conf)
    assert any("RETRY_ATTEMPTS" in w for w in warnings)


def test_an_empty_pool_is_reported():
    _, warnings = check_settings(dict(SANE, MODEL_POOL=[]))
    assert any("no fallback" in w for w in warnings)


# ── Hard errors ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("override,fragment", [
    ({"MAX_LINES_PER_CHUNK": 0}, "at least 1"),
    ({"RETRY_ATTEMPTS": -1}, "at least 1"),
    ({"PARALLEL_COOLDOWN": -5}, "cannot be negative"),
    ({"TRANSLATION_MODE": "turbo"}, "TRANSLATION_MODE"),
    ({"FILE_CONFLICT": "clobber"}, "FILE_CONFLICT"),
    ({"GEMINI_MODEL": ""}, "empty"),
    ({"MODEL_POOL": ["ok", ""]}, "non-empty model names"),
    ({"EMBED_FONT": "yes"}, "true or false"),
    ({"MAX_LINES_PER_CHUNK": True}, "not true/false"),
    ({"SOURCE_EXTENSIONS": ["srt"]}, "start with a dot"),
    ({"LANGUAGE_CODES": []}, "an object"),
])
def test_broken_values_are_errors(override, fragment):
    errors, _ = check_settings(dict(SANE, **override))
    assert any(fragment in e for e in errors), (fragment, errors)


# ── Interactions ──────────────────────────────────────────────────────────────

def test_chunk_larger_than_blob_limit_is_reported():
    conf = dict(SANE, MAX_LINES_PER_CHUNK=1000, MAX_BLOB_LINES=500)
    _, warnings = check_settings(conf)
    assert any("MAX_BLOB_LINES" in w for w in warnings)


def test_tolerance_larger_than_chunk_is_reported():
    conf = dict(SANE, MAX_LINES_PER_CHUNK=10, PARTIAL_LINE_TOLERANCE=50)
    _, warnings = check_settings(conf)
    assert any("PARTIAL_LINE_TOLERANCE" in w for w in warnings)


def test_parallel_without_cooldown_is_reported():
    conf = dict(SANE, PARALLEL_CHUNKS=4, PARALLEL_COOLDOWN=0)
    _, warnings = check_settings(conf)
    assert any("rate limiting" in w for w in warnings)


def test_same_source_and_target_language_is_reported():
    conf = dict(SANE, SOURCE_LANGUAGE="arabic", TARGET_LANGUAGE="arabic")
    _, warnings = check_settings(conf)
    assert any("nothing would change" in w for w in warnings)


def test_unmapped_language_is_reported():
    conf = dict(SANE, TARGET_LANGUAGE="klingon")
    _, warnings = check_settings(conf)
    assert any("LANGUAGE_CODES" in w for w in warnings)


# ── Prompt template ───────────────────────────────────────────────────────────

def test_a_template_contradicting_the_array_contract_is_reported():
    conf = dict(SANE, PROMPT_TEMPLATE=(
        "Translate this.\nReturn a valid JSON object with the EXACT same keys.\n"
        "{json_blob}"))
    _, warnings = check_settings(conf)
    assert any("JSON array" in w for w in warnings)


def test_a_template_without_the_placeholder_is_reported():
    _, warnings = check_settings(dict(SANE, PROMPT_TEMPLATE="Translate please."))
    assert any("json_blob" in w for w in warnings)


def test_the_built_prompt_does_not_contradict_itself():
    """The template must defer to the contract rather than specify a shape."""
    prompt = _build_prompt({"010001": "Hello"}, "Show", "english", "arabic")
    assert "EXACT same keys" not in prompt
    assert "JSON ARRAY" in prompt
    assert prompt.count("Payload:") == 1


# ── Reporting ─────────────────────────────────────────────────────────────────

def test_errors_block_the_run(isolated_settings):
    isolated_settings["TRANSLATION_MODE"] = "turbo"
    assert report_settings() is False


def test_warnings_allow_the_run_by_default(isolated_settings):
    isolated_settings["MODEL_POOL"] = ["dup", "dup"]
    isolated_settings["GEMINI_MODEL"] = "dup"
    assert report_settings() is True


def test_strict_turns_warnings_into_a_block(isolated_settings):
    isolated_settings["MODEL_POOL"] = ["dup", "dup"]
    isolated_settings["GEMINI_MODEL"] = "dup"
    assert report_settings(strict=True) is False
