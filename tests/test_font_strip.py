"""Removing an embedded font from existing output.

Embedding adds roughly 200 KB per file. Once a player supplies Arabic fonts of
its own, for example through Jellyfin's fallback font path, that payload is dead
weight and there was no way to remove it without re-translating.

Also covers a latent bug in the old stripper: a regex that ran to the end of the
file, which silently deleted [Events] in any file placing fonts before dialogue.
"""
from __future__ import annotations

import pysubs2

from btcli.fix import ALL_FIXES, _strip_fonts_section, has_embedded_font, run_fix

FONTS_LAST = """\
[Script Info]
Title: Test

[V4+ Styles]
Format: Name
Style: Default

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello

[Fonts]
fontname: Noto.ttf
!!!!encodedpayload!!!!
moreencodedpayload
"""

FONTS_FIRST = """\
[Script Info]
Title: Test

[Fonts]
fontname: Noto.ttf
!!!!encodedpayload!!!!

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello
Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,World
"""


# ── Detection ─────────────────────────────────────────────────────────────────

def test_an_embedded_font_is_detected():
    assert has_embedded_font(FONTS_LAST)
    assert has_embedded_font(FONTS_FIRST)


def test_a_file_without_a_font_is_not_flagged():
    assert not has_embedded_font("[Events]\nDialogue: 0,0,0,Default,,0,0,0,,Hi\n")


# ── Stripping ─────────────────────────────────────────────────────────────────

def test_stripping_removes_the_font_payload():
    stripped = _strip_fonts_section(FONTS_LAST)
    assert "[Fonts]" not in stripped
    assert "encodedpayload" not in stripped
    assert not has_embedded_font(stripped)


def test_stripping_keeps_the_dialogue():
    stripped = _strip_fonts_section(FONTS_LAST)
    assert "[Events]" in stripped
    assert stripped.count("Dialogue:") == 1


def test_a_font_before_the_dialogue_does_not_eat_the_events():
    """The old regex ran to end of file and would have deleted both cues."""
    stripped = _strip_fonts_section(FONTS_FIRST)
    assert "[Fonts]" not in stripped
    assert "encodedpayload" not in stripped
    assert stripped.count("Dialogue:") == 2, "dialogue must survive"
    assert "[Script Info]" in stripped


def test_stripping_a_file_with_no_font_changes_nothing():
    plain = "[Events]\nDialogue: 0,0,0,Default,,0,0,0,,Hi\n"
    assert _strip_fonts_section(plain) == plain


# ── Through the fix command ───────────────────────────────────────────────────

def test_font_strip_removes_the_font_from_a_real_file(tmp_path):
    target = tmp_path / "episode.ar.ass"
    target.write_text(FONTS_LAST, encoding="utf-8")
    before = target.stat().st_size

    run_fix(path=str(tmp_path), filter_pattern=".ar.", apply="font-strip")

    content = target.read_text(encoding="utf-8")
    assert not has_embedded_font(content)
    assert target.stat().st_size < before
    assert "Dialogue:" in content


def test_font_strip_leaves_the_subtitle_loadable(tmp_path, isolated_settings):
    """Stripping must not corrupt the file for a player."""
    isolated_settings["EMBED_FONT"] = True
    source = tmp_path / "episode.ar.ass"

    subs = pysubs2.SSAFile()
    subs.styles["Default"] = pysubs2.SSAStyle()
    event = pysubs2.SSAEvent(start=0, end=1000)
    event.text = "مرحبا"
    event.style = "Default"
    subs.append(event)
    source.write_text(subs.to_string("ass") + "\n" + """
[Fonts]
fontname: Noto.ttf
!!!!payload!!!!
""", encoding="utf-8")

    run_fix(path=str(tmp_path), filter_pattern=".ar.", apply="font-strip")

    reloaded = pysubs2.SSAFile.load(str(source))
    assert len(reloaded) == 1
    assert "مرحبا" in reloaded[0].text


def test_backup_is_kept_when_requested(tmp_path):
    target = tmp_path / "episode.ar.ass"
    target.write_text(FONTS_LAST, encoding="utf-8")

    run_fix(path=str(tmp_path), filter_pattern=".ar.",
            apply="font-strip", backup=True)

    backup = tmp_path / "episode.ar.ass.bak"
    assert backup.exists()
    assert has_embedded_font(backup.read_text(encoding="utf-8")), \
        "the backup should still contain the original font"


def test_font_strip_is_not_part_of_all():
    """'all' must not silently remove a font the user wants embedded."""
    assert "font-strip" not in ALL_FIXES


def test_font_and_font_strip_together_are_refused(tmp_path, capsys):
    target = tmp_path / "episode.ar.ass"
    target.write_text(FONTS_LAST, encoding="utf-8")

    run_fix(path=str(tmp_path), filter_pattern=".ar.", apply="font,font-strip")

    # The file must be untouched, since the request was contradictory.
    assert has_embedded_font(target.read_text(encoding="utf-8"))


def test_font_fix_removes_the_font_when_embedding_is_off(tmp_path, isolated_settings):
    """With EMBED_FONT false, the font fix strips without re-embedding."""
    isolated_settings["EMBED_FONT"] = False
    target = tmp_path / "episode.ar.ass"
    target.write_text(FONTS_LAST, encoding="utf-8")

    run_fix(path=str(tmp_path), filter_pattern=".ar.", apply="font")
    assert not has_embedded_font(target.read_text(encoding="utf-8"))


def test_an_unknown_fix_name_is_rejected(tmp_path):
    target = tmp_path / "episode.ar.ass"
    target.write_text(FONTS_LAST, encoding="utf-8")

    run_fix(path=str(tmp_path), filter_pattern=".ar.", apply="nonsense")
    assert has_embedded_font(target.read_text(encoding="utf-8")), \
        "an unknown fix must not modify anything"
