"""Job manifest and extraction reuse.

Covers the complaint that pointing btcli at an already-processed folder
re-extracted every subtitle track from scratch, and the safeguards that stop a
stale or wrong-format extraction being reused.
"""
from __future__ import annotations

import json

from btcli.manifest import ManifestRun, find_reusable_extraction


def _video(directory, name="Show - S02E01.mkv", data=b"video-v1"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


def test_a_recorded_extraction_is_reused(tmp_path):
    season = tmp_path / "Show" / "Season 02"
    video = _video(season)
    extracted = season / "Show - S02E01.en.ass"
    extracted.write_text("[Script Info]", encoding="utf-8")

    run = ManifestRun({"input_type": "vid"})
    run.register_files([video], series="Show")
    run.record_extraction(video, [2], extracted, "ass", False)
    run.finish()

    assert find_reusable_extraction(video, [2], ".en", "ass") == extracted


def test_reuse_is_refused_when_the_file_is_gone(tmp_path):
    season = tmp_path / "Show" / "Season 02"
    video = _video(season)
    extracted = season / "Show - S02E01.en.ass"
    extracted.write_text("x", encoding="utf-8")

    run = ManifestRun({"input_type": "vid"})
    run.register_files([video], series="Show")
    run.record_extraction(video, [2], extracted, "ass", False)
    run.finish()

    extracted.unlink()
    assert find_reusable_extraction(video, [2], ".en", "ass") is None


def test_reuse_is_refused_when_the_video_changed(tmp_path):
    """A replaced video must not silently reuse the old subtitles."""
    season = tmp_path / "Show" / "Season 02"
    video = _video(season)
    extracted = season / "Show - S02E01.en.ass"
    extracted.write_text("x", encoding="utf-8")

    run = ManifestRun({"input_type": "vid"})
    run.register_files([video], series="Show")
    run.record_extraction(video, [2], extracted, "ass", False)
    run.finish()

    video.write_bytes(b"a completely different encode")
    assert find_reusable_extraction(video, [2], ".en", "ass") is None


def test_reuse_is_refused_for_a_different_output_format(tmp_path):
    season = tmp_path / "Show" / "Season 02"
    video = _video(season)
    extracted = season / "Show - S02E01.en.ass"
    extracted.write_text("x", encoding="utf-8")

    run = ManifestRun({"input_type": "vid"})
    run.register_files([video], series="Show")
    run.record_extraction(video, [2], extracted, "ass", False)
    run.finish()

    assert find_reusable_extraction(video, [2], ".en", "srt") is None


def test_reuse_is_refused_for_a_different_track(tmp_path):
    season = tmp_path / "Show" / "Season 02"
    video = _video(season)
    extracted = season / "Show - S02E01.en.ass"
    extracted.write_text("x", encoding="utf-8")

    run = ManifestRun({"input_type": "vid"})
    run.register_files([video], series="Show")
    run.record_extraction(video, [2], extracted, "ass", False)
    run.finish()

    assert find_reusable_extraction(video, [3], ".en", "ass") is None


def test_each_directory_gets_its_own_numbered_jobs(tmp_path, ass_factory):
    """A run spanning two seasons writes a job into each folder."""
    root = tmp_path / "Series"
    first = ass_factory(root / "Season 01" / "e01.en.ass", [("Default", "a")])
    second = ass_factory(root / "Season 02" / "e01.en.ass", [("Default", "b")])

    run = ManifestRun({"input_type": "sub"})
    run.register_files([first, second], series="Series")
    run.finish()

    for folder, season in ((root / "Season 01", "S01"), (root / "Season 02", "S02")):
        data = json.loads((folder / ".btcli.json").read_text(encoding="utf-8"))
        assert data["series"] == "Series"
        assert data["season"] == season
        assert list(data["jobs"]) == ["job1"]


def test_a_second_run_appends_job2(tmp_path, ass_factory):
    folder = tmp_path / "Series" / "Season 01"
    sub = ass_factory(folder / "e01.en.ass", [("Default", "a")])

    for _ in range(2):
        run = ManifestRun({"input_type": "sub"})
        run.register_files([sub], series="Series")
        run.finish()

    data = json.loads((folder / ".btcli.json").read_text(encoding="utf-8"))
    assert list(data["jobs"]) == ["job1", "job2"]


def test_a_corrupt_manifest_is_replaced_not_fatal(tmp_path, ass_factory):
    folder = tmp_path / "Series" / "Season 01"
    sub = ass_factory(folder / "e01.en.ass", [("Default", "a")])
    (folder / ".btcli.json").write_text("{ broken", encoding="utf-8")

    run = ManifestRun({"input_type": "sub"})
    run.register_files([sub], series="Series")
    run.finish()

    data = json.loads((folder / ".btcli.json").read_text(encoding="utf-8"))
    assert list(data["jobs"]) == ["job1"]
