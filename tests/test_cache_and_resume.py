"""Translation cache, resume, partial files and passthrough.

Covers the loss that motivated the cache: a job ended with files incomplete and
threw away every line it had already translated. Also covers the bug where an
empty cache was falsy, so nothing was ever saved on a first run.
"""
from __future__ import annotations

import json


from btcli.cache import TranslationCache, series_root_for
from btcli.translate import run_translate
from tests.conftest import untranslated_lines


# ── Cache unit behaviour ──────────────────────────────────────────────────────

def test_empty_cache_is_truthy(tmp_path):
    """__len__ made an empty cache falsy, so first-run stores were skipped."""
    cache = TranslationCache(tmp_path, "arabic")
    assert len(cache) == 0
    assert bool(cache) is True


def test_cache_is_keyed_by_source_text_not_position(tmp_path):
    cache = TranslationCache(tmp_path, "arabic")
    cache.store_and_flush({"010001": "Hello"}, {"010001": "مرحبا"})

    reloaded = TranslationCache(tmp_path, "arabic")
    hit, missing = reloaded.split({"990099": "Hello"})
    assert hit == {"990099": "مرحبا"}, "same text under a new tag must hit"
    assert missing == {}


def test_cache_key_ignores_whitespace_differences(tmp_path):
    cache = TranslationCache(tmp_path, "arabic")
    cache.store_and_flush({"a": "Hello   there"}, {"a": "مرحبا"})
    assert TranslationCache(tmp_path, "arabic").split({"b": "Hello there"})[0]


def test_cache_is_written_during_the_job(tmp_path):
    """Entries must reach disk before any prompt, so a crash loses nothing."""
    cache = TranslationCache(tmp_path, "arabic")
    cache.store_and_flush({"a": "one"}, {"a": "واحد"})
    assert cache.path.exists()


def test_languages_do_not_overwrite_each_other(tmp_path):
    TranslationCache(tmp_path, "arabic").store_and_flush({"a": "one"}, {"a": "واحد"})
    TranslationCache(tmp_path, "french").store_and_flush({"a": "one"}, {"a": "un"})
    data = json.loads((tmp_path / ".btcli-cache.json").read_text(encoding="utf-8"))
    assert set(data["languages"]) == {"arabic", "french"}


def test_corrupt_cache_is_ignored_not_fatal(tmp_path):
    (tmp_path / ".btcli-cache.json").write_text("{ not json", encoding="utf-8")
    assert TranslationCache(tmp_path, "arabic").loaded == 0


def test_a_season_folder_shares_the_series_cache(tmp_path):
    season = tmp_path / "Show" / "Season 02"
    season.mkdir(parents=True)
    assert series_root_for([season / "e01.ass"]) == tmp_path / "Show"


def test_a_plain_folder_keeps_its_own_cache(tmp_path):
    folder = tmp_path / "Movies"
    folder.mkdir()
    assert series_root_for([folder / "m.ass"]) == folder


# ── Resume behaviour through the pipeline ─────────────────────────────────────

def test_rerun_only_sends_missing_lines(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    isolated_settings["PARTIAL_LINE_TOLERANCE"] = 10
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass",
                [("Default", "keep one"), ("Default", "keep two"),
                 ("Default", "breaks")])

    state = fake_translator(fail_texts={"breaks"})
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    assert state["sent"] == [3]

    # The cache lives at the series root, above the season folder.
    assert (tmp_path / "Show" / ".btcli-cache.json").exists()

    state["sent"].clear()
    fake_translator()                    # nothing fails this time
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    assert state["sent"] == [1], "only the previously failed line should be sent"


def test_fully_cached_rerun_sends_nothing(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass", [("Default", "one"), ("Default", "two")])

    state = fake_translator()
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    state["sent"].clear()
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    assert state["sent"] == [], "a complete cache should need no API call"


def test_no_cache_flag_retranslates_everything(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass", [("Default", "one"), ("Default", "two")])

    state = fake_translator()
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    state["sent"].clear()
    run_translate(path=str(folder), input_type="sub", lang="arabic", use_cache=False)
    assert state["sent"] == [2]


# ── Partial files ─────────────────────────────────────────────────────────────

def test_file_within_tolerance_is_written_with_source_lines(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    """Two missing lines out of many should not discard the whole file."""
    isolated_settings["PARTIAL_LINE_TOLERANCE"] = 2
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass",
                [("Default", "fine one"), ("Default", "fine two"),
                 ("Default", "bad one"), ("Default", "bad two")])

    fake_translator(fail_texts={"bad one", "bad two"})
    run_translate(path=str(folder), input_type="sub", lang="arabic")

    output = folder / "e01.ar.ass"
    assert output.exists(), "should be written despite two missing lines"
    assert len(untranslated_lines(output)) == 2


def test_file_beyond_tolerance_is_skipped(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    isolated_settings["PARTIAL_LINE_TOLERANCE"] = 1
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass",
                [("Default", "bad one"), ("Default", "bad two"),
                 ("Default", "bad three")])

    fake_translator(fail_texts={"bad one", "bad two", "bad three"})
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    assert not (folder / "e01.ar.ass").exists()


def test_resume_completes_a_partial_file(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    isolated_settings["PARTIAL_LINE_TOLERANCE"] = 5
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass",
                [("Default", "fine"), ("Default", "bad")])

    fake_translator(fail_texts={"bad"})
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    assert len(untranslated_lines(folder / "e01.ar.ass")) == 1

    fake_translator()
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    assert untranslated_lines(folder / "e01.ar.ass") == [], "file should be completed"


def test_missing_lines_report_their_text_and_files(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    """The text is needed so the lines can be listed for review."""
    isolated_settings["PARTIAL_LINE_TOLERANCE"] = 0
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass", [("Default", "shared"), ("Default", "only one")])
    ass_factory(folder / "e02.en.ass", [("Default", "shared")])

    fake_translator(fail_texts={"shared", "only one"})
    result = run_translate(path=str(folder), input_type="sub", lang="arabic")

    texts = {info["text"] for info in result["missing"].values()}
    assert texts == {"shared", "only one"}
    shared = next(i for i in result["missing"].values() if i["text"] == "shared")
    assert sorted(shared["files"]) == ["e01.en.ass", "e02.en.ass"]


# ── Passthrough ───────────────────────────────────────────────────────────────

def test_passthrough_writes_files_without_calling_the_api(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    isolated_settings["PARTIAL_LINE_TOLERANCE"] = 0
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass",
                [("Default", "fine"), ("Default", "bad one"), ("Default", "bad two")])

    state = fake_translator(fail_texts={"bad one", "bad two"})
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    assert not (folder / "e01.ar.ass").exists()

    before = state["calls"]
    run_translate(path=str(folder), input_type="sub", lang="arabic", write_only=True)

    assert state["calls"] == before, "passthrough must not call the API"
    output = folder / "e01.ar.ass"
    assert output.exists()
    assert len(untranslated_lines(output)) == 2, "failed lines stay in the source language"
