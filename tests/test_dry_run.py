"""Dry run: preview the work without spending anything.

A job can run for the better part of an hour and consume a day's request
allowance, with no way to see the shape of the work first. The guarantee tested
here is total: no API call, no output file, no manifest, no cache entry.
"""
from __future__ import annotations


from btcli.preview import _humanise, summarise
from btcli.translate import run_translate


def _folder(tmp_path, ass_factory, counts=(6, 5)):
    folder = tmp_path / "Show" / "Season 01"
    for index, count in enumerate(counts, 1):
        ass_factory(folder / f"e{index:02d}.en.ass",
                    [("Default", f"file{index} line {n}") for n in range(count)])
    return folder


# ── The guarantee ─────────────────────────────────────────────────────────────

def test_a_dry_run_makes_no_api_call(tmp_path, ass_factory, fake_translator):
    folder = _folder(tmp_path, ass_factory)
    state = fake_translator()

    run_translate(path=str(folder), input_type="sub", lang="arabic", dry_run=True)
    assert state["calls"] == 0


def test_a_dry_run_writes_no_output(tmp_path, ass_factory, fake_translator):
    folder = _folder(tmp_path, ass_factory)
    fake_translator()

    run_translate(path=str(folder), input_type="sub", lang="arabic", dry_run=True)
    assert list(folder.glob("*.ar.ass")) == []


def test_a_dry_run_writes_no_manifest(tmp_path, ass_factory, fake_translator):
    folder = _folder(tmp_path, ass_factory)
    fake_translator()

    run_translate(path=str(folder), input_type="sub", lang="arabic", dry_run=True)
    assert not (folder / ".btcli.json").exists()


def test_a_dry_run_writes_no_cache(tmp_path, ass_factory, fake_translator):
    folder = _folder(tmp_path, ass_factory)
    fake_translator()

    run_translate(path=str(folder), input_type="sub", lang="arabic", dry_run=True)
    assert not (tmp_path / "Show" / ".btcli-cache.json").exists()


def test_a_dry_run_needs_no_api_key(tmp_path, ass_factory, fake_translator,
                                    isolated_settings):
    """Without a key a normal run prompts; a dry run must simply proceed."""
    isolated_settings["GEMINI_API_KEY"] = ""
    folder = _folder(tmp_path, ass_factory)
    fake_translator()

    result = run_translate(path=str(folder), input_type="sub", lang="arabic",
                           dry_run=True)
    assert result["dry_run"] is True


def test_a_real_run_still_writes_after_a_dry_run(
        tmp_path, ass_factory, fake_translator):
    """A dry run must not poison the following real run."""
    folder = _folder(tmp_path, ass_factory, counts=(3,))
    fake_translator()

    run_translate(path=str(folder), input_type="sub", lang="arabic", dry_run=True)
    run_translate(path=str(folder), input_type="sub", lang="arabic")
    assert (folder / "e01.ar.ass").exists()


# ── The numbers reported ──────────────────────────────────────────────────────

def test_the_preview_counts_cues_and_deduplication(
        tmp_path, ass_factory, fake_translator):
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass",
                [("Default", "same"), ("Default", "same"), ("Default", "other")])
    fake_translator()

    result = run_translate(path=str(folder), input_type="sub", lang="arabic",
                           dry_run=True)
    preview = result["previews"][0]
    assert preview["cues"] == 3
    assert preview["unique"] == 2, "the repeated line collapses"
    assert preview["deduplicated"] == 1


def test_the_preview_counts_requests_from_the_chunk_size(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    isolated_settings["MAX_LINES_PER_CHUNK"] = 4
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass",
                [("Default", f"line {n}") for n in range(12)])
    fake_translator()

    result = run_translate(path=str(folder), input_type="sub", lang="arabic",
                           dry_run=True)
    preview = result["previews"][0]
    assert preview["requests"] == 3
    assert preview["chunk_sizes"] == [4, 4, 4]


def test_the_preview_names_the_files_that_would_be_written(
        tmp_path, ass_factory, fake_translator):
    folder = _folder(tmp_path, ass_factory, counts=(2, 2))
    fake_translator()

    result = run_translate(path=str(folder), input_type="sub", lang="arabic",
                           dry_run=True)
    outputs = [entry["output"] for entry in result["previews"][0]["outputs"]]
    assert outputs == ["e01.ar.ass", "e02.ar.ass"]


def test_cached_lines_reduce_the_work_reported(
        tmp_path, ass_factory, fake_translator):
    """After a real run, a dry run should show there is nothing left to send."""
    folder = _folder(tmp_path, ass_factory, counts=(4,))
    fake_translator()
    run_translate(path=str(folder), input_type="sub", lang="arabic")

    result = run_translate(path=str(folder), input_type="sub", lang="arabic",
                           dry_run=True)
    preview = result["previews"][0]
    assert preview["cached"] == 4
    assert preview["to_translate"] == 0
    assert preview["requests"] == 0


def test_the_minimum_duration_comes_from_the_cooldown(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    isolated_settings["MAX_LINES_PER_CHUNK"] = 2
    isolated_settings["PARALLEL_COOLDOWN"] = 30
    isolated_settings["PARALLEL_CHUNKS"] = 1
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass", [("Default", f"line {n}") for n in range(6)])
    fake_translator()

    result = run_translate(path=str(folder), input_type="sub", lang="arabic",
                           dry_run=True)
    preview = result["previews"][0]
    # Three requests means two waits between them.
    assert preview["requests"] == 3
    assert preview["minimum_seconds"] == 60


def test_parallel_chunks_reduce_the_batch_count(isolated_settings):
    isolated_settings["PARALLEL_CHUNKS"] = 3
    isolated_settings["PARALLEL_COOLDOWN"] = 30
    summary = summarise(
        files=["a.ass"], stats={"total": 9, "unique": 9, "collapsed": 0, "pct": 0},
        cached=0, chunks=[{"1": "a"}] * 6, required_by_file={1: {"1"}},
        tolerance=10, suffix=".ar", source_lang="english", target_lang="arabic",
        mode="chunked")
    assert summary["requests"] == 6
    assert summary["batches"] == 2
    assert summary["minimum_seconds"] == 30


# ── Video input ───────────────────────────────────────────────────────────────

def test_video_input_stops_before_extraction(tmp_path, fake_translator,
                                             isolated_settings):
    """Extraction writes files, so a dry run must not run it."""
    folder = tmp_path / "Show" / "Season 01"
    folder.mkdir(parents=True)
    (folder / "e01.mkv").write_bytes(b"not really a video")
    fake_translator()

    result = run_translate(path=str(folder), input_type="vid", lang="arabic",
                           dry_run=True)
    assert result["dry_run"] is True
    assert list(folder.glob("*.en.*")) == [], "nothing should be extracted"


# ── Formatting ────────────────────────────────────────────────────────────────

def test_durations_read_naturally():
    assert _humanise(0) == "0s"
    assert _humanise(45) == "45s"
    assert _humanise(60) == "1m 00s"
    assert _humanise(150) == "2m 30s"
    assert _humanise(3600) == "1h 00m"
    assert _humanise(3900) == "1h 05m"
    assert _humanise(-10) == "0s"
