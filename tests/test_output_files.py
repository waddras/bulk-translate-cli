"""Output assembly: line breaks, RTL wrapping, and self-translation.

Covers the SRT bug where a two-line cue was written as one physical line and the
bidi engine then displayed the second line first, and the bug where a previous
run's own output was picked up as a source and translated again.
"""
from __future__ import annotations

from pathlib import Path

import pysubs2

from btcli.discover import discover_files, exclude_translated_output, group_by_directory
from btcli.sub_post import PDI, RLI, build_srt_output, wrap_rtl


def test_srt_uses_real_newlines_not_ass_breaks():
    """A literal \\N in SRT merged both lines and reversed their order."""
    wrapped = wrap_rtl("hello jake\nmy name is kamal", "\n")
    assert wrapped == f"{RLI}hello jake{PDI}\n{RLI}my name is kamal{PDI}"
    assert r"\N" not in wrapped


def test_ass_still_uses_backslash_n():
    wrapped = wrap_rtl("hello jake\nmy name is kamal", r"\N")
    assert r"\N" in wrapped
    assert "\n" not in wrapped


def test_each_line_is_isolated_so_order_survives_bidi():
    """Per-line isolation is what stops line two rendering before line one."""
    wrapped = wrap_rtl("first\nsecond", "\n")
    for line in wrapped.split("\n"):
        assert line.startswith(RLI) and line.endswith(PDI)


def test_srt_body_contains_a_real_two_line_cue():
    block = {"start": 1000, "end": 2000,
             "text": wrap_rtl("one\ntwo", "\n"), "block_idx": 1}
    body = build_srt_output([block]).split("\n", 2)[2]
    assert r"\N" not in body
    assert body.strip().count("\n") == 1


def test_previous_output_is_not_treated_as_a_source():
    """Re-running produced name.ar.ar.ass and re-billed the whole file."""
    sources, outputs = exclude_translated_output(
        [Path("ep.en.ass"), Path("ep.ar.ass"), Path("ep.ar.srt")], ".ar")
    assert [p.name for p in sources] == ["ep.en.ass"]
    assert len(outputs) == 2


def test_discovery_stops_one_directory_deep(tmp_path, ass_factory):
    root = tmp_path / "Show"
    ass_factory(root / "Season 01" / "e01.en.ass", [("Default", "a")])
    ass_factory(root / "Season 01" / "Extra" / "nested.en.ass", [("Default", "b")])
    found = [p.name for p in discover_files(str(root), mode="sub")]
    assert "e01.en.ass" in found
    assert "nested.en.ass" not in found, "must not walk deeper than one level"


def test_group_by_directory_keeps_folders_separate(series):
    grouped = group_by_directory(str(series), mode="sub")
    names = sorted(d.name for d in grouped)
    assert names == ["Season 01", "Season 02"]


def test_ass_output_preserves_line_breaks_end_to_end(
        tmp_path, ass_factory, fake_translator, isolated_settings):
    """A translated multi-line ASS cue must still contain \\N."""
    from btcli.translate import run_translate

    fake_translator()
    folder = tmp_path / "Show" / "Season 01"
    ass_factory(folder / "e01.en.ass", [("Default", r"Hurry up!\NWe are late")])

    run_translate(path=str(folder), input_type="sub", lang="arabic")

    out = pysubs2.SSAFile.load(str(folder / "e01.ar.ass"))
    assert r"\N" in out[0].text, "ASS break lost in the round trip"


def test_srt_output_is_two_physical_lines_end_to_end(
        tmp_path, srt_factory, fake_translator, isolated_settings):
    from btcli.translate import run_translate

    fake_translator()
    folder = tmp_path / "Show" / "Season 01"
    srt_factory(folder / "e01.en.srt", ["hello jake\nmy name is kamal"])

    run_translate(path=str(folder), input_type="sub", lang="arabic")

    body = (folder / "e01.ar.srt").read_text(encoding="utf-8")
    assert r"\N" not in body, "SRT must not carry ASS line breaks"
