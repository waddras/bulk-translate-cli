"""Repairing a settings.conf that has gone stale.

Because btcli never overwrites a user's values, a setting can keep working while
no longer matching what the code expects. Two real cases from one install drive
these tests: a PROMPT_TEMPLATE still demanding a JSON object after the code
moved to arrays, and GEMINI_MODEL declared twice, where JSON silently keeps the
last value and reports nothing.

The repairs edit raw text rather than re-serialising, so comments, ordering and
formatting survive. That is the part most worth testing.
"""
from __future__ import annotations

import json

import pytest

from btcli.update import UpdateError, dedupe_settings, reset_settings
from btcli.validate import find_duplicate_keys

# Mirrors the install that prompted this: comments, a blank-line-separated
# duplicate, a multi-line list, a nested object, and a stale prompt.
REPORTED_CONFIG = '''{
  // ═══ API CONFIGURATION ═══
  "GEMINI_API_KEY_FILE": "~/.btcli.env",

  // primary model
  "GEMINI_MODEL": "gemini-3.5-flash-lite",
  "GEMINI_MODEL": "gemini-3.5-flash-lite",

  "MODEL_POOL": [
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash"
  ],

  "LANGUAGE_CODES": {
    "arabic": "ar",
    "french": "fr"
  },

  // the stale one
  "PROMPT_TEMPLATE": "Translate.\\nReturn a valid JSON object with the EXACT same keys.\\n\\n{json_blob}",

  "MAX_FAILED_CHUNKS": 2
}
'''

SHIPPED = {
    "GEMINI_API_KEY_FILE": "~/.btcli.env",
    "GEMINI_MODEL": "gemini-3.1-flash-lite",
    "MODEL_POOL": ["default-a", "default-b"],
    "LANGUAGE_CODES": {"arabic": "ar"},
    "PROMPT_TEMPLATE": "Clean.\nThe contract sets the reply format.\n\n{json_blob}",
    "MAX_FAILED_CHUNKS": 5,
    # Shipped but not set by the user, so reset has nothing to replace.
    "FILE_CONFLICT": "overwrite",
}


@pytest.fixture
def config(tmp_path):
    """A (user_file, default_file) pair. Returns them plus a parse helper."""
    user = tmp_path / "settings.conf"
    user.write_text(REPORTED_CONFIG, encoding="utf-8")
    defaults = tmp_path / "settings.default.conf"
    defaults.write_text(json.dumps(SHIPPED, indent=2), encoding="utf-8")
    return user, defaults


def parse(path) -> dict:
    from btcli.update import _load_json_with_comments
    return _load_json_with_comments(path)


# ── finding duplicates ────────────────────────────────────────────────────────

def test_duplicate_key_is_found():
    assert find_duplicate_keys(REPORTED_CONFIG) == ["GEMINI_MODEL"]


def test_a_clean_config_has_no_duplicates():
    assert find_duplicate_keys('{"A": 1, "B": 2}') == []


def test_the_same_key_nested_elsewhere_is_not_a_duplicate():
    """LANGUAGE_CODES.arabic and a top-level arabic would be different settings."""
    assert find_duplicate_keys('{"arabic": 1, "CODES": {"arabic": 2}}') == []


def test_duplicates_inside_a_nested_object_are_found():
    assert find_duplicate_keys('{"CODES": {"ar": 1, "ar": 2}}') == ["ar"]


def test_unparseable_config_reports_no_duplicates():
    """Bad syntax is reported by the loader; this must not raise on the way."""
    assert find_duplicate_keys('{"A": oops}') == []


def test_comments_do_not_create_false_duplicates():
    raw = '{\n  // "A": 1 is commented out\n  "A": 2\n}'
    assert find_duplicate_keys(raw) == []


# ── reset ─────────────────────────────────────────────────────────────────────

def test_reset_replaces_the_stale_prompt(config):
    user, defaults = config
    reset_settings(user, defaults, ["PROMPT_TEMPLATE"])

    assert parse(user)["PROMPT_TEMPLATE"] == SHIPPED["PROMPT_TEMPLATE"]
    assert "EXACT same keys" not in user.read_text()


def test_reset_leaves_every_other_value_alone(config):
    user, defaults = config
    reset_settings(user, defaults, ["PROMPT_TEMPLATE"])

    result = parse(user)
    assert result["MAX_FAILED_CHUNKS"] == 2, "user's value, not the default 5"
    assert result["MODEL_POOL"] == ["gemini-3.1-flash-lite", "gemini-3.7-flash"]
    assert result["LANGUAGE_CODES"] == {"arabic": "ar", "french": "fr"}


def test_reset_keeps_comments(config):
    user, defaults = config
    reset_settings(user, defaults, ["PROMPT_TEMPLATE"])

    text = user.read_text()
    assert "// ═══ API CONFIGURATION ═══" in text
    assert "// primary model" in text


def test_reset_collapses_a_duplicated_key(config):
    user, defaults = config
    summary = reset_settings(user, defaults, ["GEMINI_MODEL"])

    text = user.read_text()
    assert text.count('"GEMINI_MODEL"') == 1
    assert summary["collapsed"] == ["GEMINI_MODEL"]
    assert parse(user)["GEMINI_MODEL"] == SHIPPED["GEMINI_MODEL"]


def test_reset_handles_several_keys_at_once(config):
    user, defaults = config
    reset_settings(user, defaults, ["PROMPT_TEMPLATE", "MAX_FAILED_CHUNKS"])

    result = parse(user)
    assert result["PROMPT_TEMPLATE"] == SHIPPED["PROMPT_TEMPLATE"]
    assert result["MAX_FAILED_CHUNKS"] == 5


def test_reset_writes_a_backup(config):
    user, defaults = config
    original = user.read_text()
    summary = reset_settings(user, defaults, ["PROMPT_TEMPLATE"])

    assert summary["backup"].read_text() == original


def test_reset_result_is_still_parseable(config):
    user, defaults = config
    reset_settings(user, defaults, ["PROMPT_TEMPLATE", "GEMINI_MODEL",
                                    "MODEL_POOL", "LANGUAGE_CODES"])
    assert isinstance(parse(user), dict)
    assert find_duplicate_keys(user.read_text()) == []


def test_reset_rejects_an_unknown_setting_without_touching_the_file(config):
    user, defaults = config
    original = user.read_text()

    with pytest.raises(UpdateError, match="Not a known setting"):
        reset_settings(user, defaults, ["PROMPT_TEMPLATE", "NOT_A_SETTING"])

    assert user.read_text() == original, "an unknown name must change nothing"


def test_reset_reports_a_key_the_user_never_set(config):
    """Nothing to replace, because the built-in default is already in force."""
    user, defaults = config
    original = user.read_text()

    summary = reset_settings(user, defaults, ["FILE_CONFLICT"])

    assert summary["absent"] == ["FILE_CONFLICT"]
    assert summary["reset"] == {}
    assert user.read_text() == original, "nothing to do, so write nothing"


def test_reset_needs_a_defaults_file(tmp_path):
    user = tmp_path / "settings.conf"
    user.write_text("{}", encoding="utf-8")
    with pytest.raises(UpdateError, match="not found"):
        reset_settings(user, tmp_path / "missing.conf", ["GEMINI_MODEL"])


def test_reset_needs_the_user_file_to_exist(tmp_path):
    defaults = tmp_path / "settings.default.conf"
    defaults.write_text(json.dumps(SHIPPED), encoding="utf-8")
    with pytest.raises(UpdateError, match="does not exist"):
        reset_settings(tmp_path / "nope.conf", defaults, ["GEMINI_MODEL"])


# ── dedupe ────────────────────────────────────────────────────────────────────

def test_dedupe_collapses_the_duplicate(config):
    user, _ = config
    summary = dedupe_settings(user)

    assert user.read_text().count('"GEMINI_MODEL"') == 1
    assert summary["removed"]["GEMINI_MODEL"]["dropped"] == 1
    assert find_duplicate_keys(user.read_text()) == []


def test_dedupe_keeps_the_users_value_not_the_default(config):
    """The whole point: --dedupe must not change what btcli actually does."""
    user, _ = config
    before = parse(user)

    dedupe_settings(user)

    assert parse(user)["GEMINI_MODEL"] == "gemini-3.5-flash-lite"
    assert parse(user) == before, "effective settings must be byte-for-byte equal"


def test_dedupe_keeps_the_last_declaration_because_json_does(tmp_path):
    """When the two values differ, the surviving one must be the effective one."""
    user = tmp_path / "settings.conf"
    user.write_text('{\n  "GEMINI_MODEL": "old",\n  "GEMINI_MODEL": "new"\n}\n',
                    encoding="utf-8")

    dedupe_settings(user)

    assert parse(user)["GEMINI_MODEL"] == "new"
    assert "old" not in user.read_text()


def test_dedupe_keeps_comments_and_other_settings(config):
    user, _ = config
    dedupe_settings(user)

    text = user.read_text()
    assert "// primary model" in text
    assert "// the stale one" in text
    assert parse(user)["MAX_FAILED_CHUNKS"] == 2


def test_dedupe_is_a_no_op_on_a_clean_config(tmp_path):
    user = tmp_path / "settings.conf"
    user.write_text('{\n  "A": 1\n}\n', encoding="utf-8")
    original = user.read_text()

    summary = dedupe_settings(user)

    assert summary["removed"] == {}
    assert summary["backup"] is None
    assert user.read_text() == original, "nothing to do, so write nothing"


def test_dedupe_writes_a_backup(config):
    user, _ = config
    original = user.read_text()
    summary = dedupe_settings(user)
    assert summary["backup"].read_text() == original


def test_dedupe_preview_does_not_write(config):
    user, _ = config
    original = user.read_text()

    summary = dedupe_settings(user, apply=False)

    assert summary["removed"], "still reports what it would do"
    assert user.read_text() == original


# ── the reported case, end to end ─────────────────────────────────────────────

def test_the_reported_config_is_fully_repaired(config):
    """Both problems from the real install, fixed the way I would advise."""
    user, defaults = config

    dedupe_settings(user)                                  # keep my model choice
    reset_settings(user, defaults, ["PROMPT_TEMPLATE"])    # drop the stale prompt

    text = user.read_text()
    result = parse(user)

    assert find_duplicate_keys(text) == [], "no duplicate keys left"
    assert result["GEMINI_MODEL"] == "gemini-3.5-flash-lite", "my choice survived"
    assert "EXACT same keys" not in text, "contradiction gone"
    assert result["PROMPT_TEMPLATE"] == SHIPPED["PROMPT_TEMPLATE"]
    assert result["MAX_FAILED_CHUNKS"] == 2, "my other tuning survived"
    assert "// primary model" in text, "comments survived"
