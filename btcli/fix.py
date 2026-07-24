"""Fix flow: re-process translated subtitle files without hitting the API.

Available fixes:
  - rtl: re-wrap dialogue lines with RLI+PDI
  - font: re-embed font using correct ASS UUEncode format
  - style: re-apply font style settings from config
  - linebreak: ASS -> convert real newlines to \\N; SRT -> convert literal \\N
    back to real newlines
  - all: apply all fixes
"""
from __future__ import annotations

import re
from pathlib import Path

import pysubs2

from .config import cfg
from .discover import discover_files
from .logger import log
from .sub_post import (
    RLI, PDI, wrap_rtl, embed_font_in_ass, _build_ass_style,
)

AVAILABLE_FIXES = ["rtl", "font", "style", "linebreak", "all"]


def run_fix(path: str, filter_pattern: str | None = None,
            apply: str = "all", backup: bool = False) -> None:
    """Run fix flow on existing translated subtitle files.

    Args:
        path: file or directory
        filter_pattern: filename filter
        apply: comma-separated fix names or "all"
        backup: if True, save .bak before overwriting
    """
    fixes = [f.strip() for f in apply.split(",")]
    if "all" in fixes:
        fixes = ["rtl", "font", "style", "linebreak"]

    # Validate
    for f in fixes:
        if f not in AVAILABLE_FIXES:
            log.error(f"Unknown fix: '{f}'. Available: {', '.join(AVAILABLE_FIXES)}")
            return

    # Discover files
    files = discover_files(path, mode="sub", scan_mode="recursive", filter_pattern=filter_pattern)
    if not files:
        log.error(f"No subtitle files found in: {path}")
        return

    log.sep()
    log.phase(f"FIX - {len(files)} file(s)")
    log.info(f"  Applying: {', '.join(fixes)}")
    log.sep()

    fixed_count = 0
    for i, fpath in enumerate(files, 1):
        fpath = Path(fpath)
        log.item(f"[{i:02d}] {fpath.name}")

        try:
            if fpath.suffix.lower() in (".ass", ".ssa"):
                _fix_ass_file(fpath, fixes, backup)
            else:
                _fix_srt_file(fpath, fixes, backup)
            fixed_count += 1
        except Exception as e:
            log.error(f"  Failed: {e}")

    log.sep()
    log.success(f"FIX COMPLETE - {fixed_count}/{len(files)} files processed")


def _fix_ass_file(fpath: Path, fixes: list, backup: bool) -> None:
    """Apply fixes to an ASS file."""
    content = fpath.read_text(encoding="utf-8")

    if backup:
        bak = fpath.with_suffix(fpath.suffix + ".bak")
        bak.write_text(content, encoding="utf-8")

    # Parse with pysubs2 for style/event manipulation
    subs = pysubs2.SSAFile.from_string(content)
    modified = False

    # Fix: style — re-apply font settings from config
    if "style" in fixes:
        new_style = _build_ass_style()
        subs.styles["Default"] = new_style
        modified = True
        log.detail(f"        Applied style fix")

    # Fix: linebreak — convert literal newlines to \N
    if "linebreak" in fixes:
        for event in subs:
            if "\n" in event.text:
                event.text = event.text.replace("\n", r"\N")
                modified = True
        log.detail(f"        Applied linebreak fix")

    # Fix: rtl — re-wrap with RLI+PDI
    if "rtl" in fixes:
        for event in subs:
            # Strip existing RLI/PDI marks first
            text = event.text.replace(RLI, "").replace(PDI, "")
            # Split on \N, wrap each part
            parts = text.split(r"\N")
            wrapped = r"\N".join(RLI + p + PDI for p in parts)
            if event.text != wrapped:
                event.text = wrapped
                modified = True
        log.detail(f"        Applied RTL fix")

    if modified:
        content = subs.to_string("ass")

    # Fix: font — re-embed font
    if "font" in fixes:
        # Remove existing [Fonts] section if present
        content = _strip_fonts_section(content)
        if cfg.get("EMBED_FONT", True):
            content = embed_font_in_ass(content)
            log.detail(f"        Applied font fix")

    fpath.write_text(content, encoding="utf-8")


def _fix_srt_file(fpath: Path, fixes: list, backup: bool) -> None:
    """Apply fixes to an SRT file.

    linebreak: split any literal ``\\N`` (an ASS break wrongly written into SRT)
               into real physical lines so multi-line cues stop merging.
    rtl:       strip and re-wrap each physical text line with RLI+PDI so bidi
               reordering never flips the on-screen line order.
    """
    content = fpath.read_text(encoding="utf-8")

    do_linebreak = "linebreak" in fixes
    do_rtl = "rtl" in fixes
    if not (do_linebreak or do_rtl):
        return

    if backup:
        bak = fpath.with_suffix(fpath.suffix + ".bak")
        bak.write_text(content, encoding="utf-8")

    modified = False
    new_lines = []
    for line in content.split("\n"):
        stripped = line.strip()
        # Preserve sequence numbers, timestamps, and blank lines verbatim.
        if not stripped or stripped.isdigit() or "-->" in stripped:
            new_lines.append(line)
            continue

        segments = stripped.split(r"\N") if do_linebreak else [stripped]
        rebuilt = []
        for seg in segments:
            seg = seg.strip()
            if do_rtl:
                seg = RLI + seg.replace(RLI, "").replace(PDI, "") + PDI
            rebuilt.append(seg)
        replacement = "\n".join(rebuilt)
        new_lines.append(replacement)
        if replacement != line:
            modified = True

    if modified:
        fpath.write_text("\n".join(new_lines), encoding="utf-8")


def _strip_fonts_section(content: str) -> str:
    """Remove existing [Fonts] section from ASS content."""
    # [Fonts] section goes to end of file or next section
    pattern = r'\n?\[Fonts\]\n.*'
    return re.sub(pattern, '', content, flags=re.DOTALL)
