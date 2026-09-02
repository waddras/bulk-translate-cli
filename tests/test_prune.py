"""Pruning the state files btcli leaves beside media.

The cache accumulates every line ever translated for a series and the manifest
every job ever run, with no way to inspect or trim either.

The safety property that matters: a cached line whose source text still exists
must never be removed, because that is what makes resuming cheap.
"""
from __future__ import annotations

import json

import pytest

from btcli.cache import TranslationCache
from btcli.manifest import ManifestRun
from btcli.prune import find_state_files, prune_cache, prune_manifests, run_prune


@pytest.fixture
def series_with_state(tmp_path, ass_factory):
    """A series whose cache holds both live and stale entries."""
    show = tmp_path / "Show"
    season = show / "Season 01"
    ass_factory(season / "e01.en.ass",
                [("Default", "alive one"), ("Default", "alive two")])

    cache = TranslationCache(show, "arabic")
    cache.store_and_flush({"a": "alive one", "b": "alive two"},
                          {"a": "AR alive one", "b": "AR alive two"})
    cache.store_and_flush({"x": "deleted one", "y": "deleted two"},
                          {"x": "AR gone one", "y": "AR gone two"})
    return show, season


def _jobs(season):
    return json.loads((season / ".btcli.json").read_text(encoding="utf-8"))["jobs"]


def _add_jobs(season, source, count):
    for _ in range(count):
        run = ManifestRun({"input_type": "sub"})
        run.register_files([source], series="Show")
        run.finish()


# ── Discovery ─────────────────────────────────────────────────────────────────

def test_state_files_are_found_at_any_depth(series_with_state):
    show, season = series_with_state
    _add_jobs(season, season / "e01.en.ass", 1)

    assert len(find_state_files(show, ".btcli-cache.json")) == 1
    assert len(find_state_files(show, ".btcli.json")) == 1


def test_hidden_tooling_directories_are_skipped(tmp_path):
    junk = tmp_path / ".git"
    junk.mkdir()
    (junk / ".btcli-cache.json").write_text("{}", encoding="utf-8")
    assert find_state_files(tmp_path, ".btcli-cache.json") == []


# ── Cache pruning ─────────────────────────────────────────────────────────────

def test_preview_removes_nothing(series_with_state):
    show, _ = series_with_state
    result = prune_cache(show, "arabic", apply=False)
    assert result["removed"] == 2
    assert len(TranslationCache(show, "arabic")) == 4, "preview must not delete"


def test_applying_removes_only_stale_entries(series_with_state):
    show, _ = series_with_state
    prune_cache(show, "arabic", apply=True)

    cache = TranslationCache(show, "arabic")
    assert len(cache) == 2


def test_a_line_still_in_a_source_is_never_removed(series_with_state):
    """This is what keeps resuming cheap, so it must hold."""
    show, _ = series_with_state
    prune_cache(show, "arabic", apply=True)

    cache = TranslationCache(show, "arabic")
    hit, missing = cache.split({"any-tag": "alive one"})
    assert hit == {"any-tag": "AR alive one"}
    assert missing == {}


def test_translated_output_is_not_counted_as_a_source(tmp_path, ass_factory):
    """Only source subtitles keep a line alive, not this tool's own output."""
    show = tmp_path / "Show"
    season = show / "Season 01"
    ass_factory(season / "e01.ar.ass", [("Default", "only in output")])

    cache = TranslationCache(show, "arabic")
    cache.store_and_flush({"a": "only in output"}, {"a": "AR"})

    prune_cache(show, "arabic", apply=True)
    assert len(TranslationCache(show, "arabic")) == 0


def test_another_language_is_left_alone(series_with_state):
    show, _ = series_with_state
    french = TranslationCache(show, "french")
    french.store_and_flush({"a": "deleted one"}, {"a": "FR"})

    prune_cache(show, "arabic", apply=True)
    assert len(TranslationCache(show, "french")) == 1, "only the named language is pruned"


def test_pruning_a_missing_cache_is_harmless(tmp_path):
    result = prune_cache(tmp_path, "arabic", apply=True)
    assert result["removed"] == 0


# ── Manifest pruning ──────────────────────────────────────────────────────────

def test_only_the_most_recent_jobs_are_kept(series_with_state):
    show, season = series_with_state
    _add_jobs(season, season / "e01.en.ass", 12)

    prune_manifests(show, keep=3, apply=True)
    assert len(_jobs(season)) == 3


def test_remaining_jobs_are_renumbered_without_holes(series_with_state):
    show, season = series_with_state
    _add_jobs(season, season / "e01.en.ass", 12)

    prune_manifests(show, keep=3, apply=True)
    assert list(_jobs(season)) == ["job1", "job2", "job3"]


def test_the_newest_jobs_are_the_ones_kept(series_with_state):
    show, season = series_with_state
    _add_jobs(season, season / "e01.en.ass", 5)
    before = _jobs(season)
    newest = before["job5"]["started_at"]

    prune_manifests(show, keep=2, apply=True)
    after = _jobs(season)
    assert after["job2"]["started_at"] == newest, "the latest job must survive"


def test_manifest_preview_removes_nothing(series_with_state):
    show, season = series_with_state
    _add_jobs(season, season / "e01.en.ass", 8)

    result = prune_manifests(show, keep=2, apply=False)
    assert result["removed"] == 6
    assert len(_jobs(season)) == 8


def test_history_shorter_than_keep_is_untouched(series_with_state):
    show, season = series_with_state
    _add_jobs(season, season / "e01.en.ass", 2)

    prune_manifests(show, keep=10, apply=True)
    assert len(_jobs(season)) == 2


# ── Command entry point ───────────────────────────────────────────────────────

def test_run_prune_reports_without_applying(series_with_state, capsys):
    show, season = series_with_state
    _add_jobs(season, season / "e01.en.ass", 6)

    run_prune(path=str(show), what="all", keep=2, apply=False)
    assert len(TranslationCache(show, "arabic")) == 4
    assert len(_jobs(season)) == 6


def test_run_prune_can_target_only_the_manifest(series_with_state):
    show, season = series_with_state
    _add_jobs(season, season / "e01.en.ass", 6)

    run_prune(path=str(show), what="manifest", keep=2, apply=True)
    assert len(_jobs(season)) == 2
    assert len(TranslationCache(show, "arabic")) == 4, "cache should be untouched"


def test_run_prune_can_target_only_the_cache(series_with_state):
    show, season = series_with_state
    _add_jobs(season, season / "e01.en.ass", 6)

    run_prune(path=str(show), what="cache", keep=2, apply=True)
    assert len(TranslationCache(show, "arabic")) == 2
    assert len(_jobs(season)) == 6, "manifest should be untouched"


def test_an_unknown_target_is_rejected(series_with_state):
    show, _ = series_with_state
    run_prune(path=str(show), what="nonsense", apply=True)
    assert len(TranslationCache(show, "arabic")) == 4, "nothing should change"


def test_a_missing_path_is_reported_not_raised(tmp_path):
    run_prune(path=str(tmp_path / "nope"), what="all", apply=True)
