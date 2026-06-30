"""Style argument parsing and karaoke detection.

Syntax for -s flag:
  - StyleName       → translate this style
  - +StyleName      → passthrough this style (untouched)
  - ALL             → translate all styles
  - +ALL            → passthrough all styles not explicitly listed for translate
  - +karaoke        → auto-detect karaoke styles and passthrough them

Examples:
  -s "Default,Default-Alt,+karaoke"
  -s "ALL,+karaoke"
  -s "Default,+ALL"
  -s "Default,+Signs,+Card,+karaoke"
"""
from __future__ import annotations

import pysubs2
from pathlib import Path

from .config import cfg
from .logger import log
from .srt_pre import _clean_event_text


def parse_styles_arg(arg: str) -> tuple:
    """Parse -s argument into (keep_styles, passthrough_styles).

    Returns:
        (keep_styles, passthrough_styles) where each is a list or None.
        Special values:
          keep_styles = ["ALL"] means translate all
          keep_styles = None means use auto-detection (KEEP_TOP_STYLES)
    """
    parts = [p.strip() for p in arg.split(",") if p.strip()]

    translate = []
    passthrough = []
    has_all = False
    has_plus_all = False
    has_plus_karaoke = False

    for part in parts:
        if part == "ALL":
            has_all = True
        elif part == "+ALL":
            has_plus_all = True
        elif part == "+karaoke":
            has_plus_karaoke = True
        elif part.startswith("+"):
            passthrough.append(part[1:])
        else:
            translate.append(part)

    # Store flags for resolve_styles to use
    return _StyleConfig(
        translate=translate,
        passthrough=passthrough,
        has_all=has_all,
        has_plus_all=has_plus_all,
        has_plus_karaoke=has_plus_karaoke,
    ).resolve()


class _StyleConfig:
    """Internal helper to resolve style configuration."""

    def __init__(self, translate, passthrough, has_all, has_plus_all, has_plus_karaoke):
        self.translate = translate
        self.passthrough = passthrough
        self.has_all = has_all
        self.has_plus_all = has_plus_all
        self.has_plus_karaoke = has_plus_karaoke

    def resolve(self) -> tuple:
        """Resolve into (keep_styles, passthrough_styles).

        Note: karaoke detection and +ALL resolution need file context,
        so we return markers that translate.py will resolve with actual files.
        """
        keep = self.translate if self.translate else None
        passthru = self.passthrough if self.passthrough else None

        if self.has_all:
            keep = ["ALL"]
        if self.has_plus_all:
            passthru = (passthru or []) + ["+ALL"]
        if self.has_plus_karaoke:
            passthru = (passthru or []) + ["+karaoke"]

        return keep, passthru


def detect_karaoke_styles(files: list) -> list:
    """Detect karaoke styles from subtitle files.

    A style is karaoke if the majority of its events have:
      - Single-char cleaned text (1-3 chars), OR
      - [fx] in the effect field

    Returns list of style names detected as karaoke.
    """
    style_stats: dict = {}  # {style: {total: int, karaoke: int}}

    for fpath in files:
        try:
            subs = pysubs2.SSAFile.load(str(fpath))
        except Exception:
            continue

        for event in subs:
            style = getattr(event, "style", "Default")
            if style not in style_stats:
                style_stats[style] = {"total": 0, "karaoke": 0}

            style_stats[style]["total"] += 1

            # Check for karaoke indicators
            is_karaoke = False

            # Check effect field for [fx]
            effect = getattr(event, "effect", "")
            if "[fx]" in effect.lower():
                is_karaoke = True

            # Check for single-char cleaned text
            if not is_karaoke:
                clean = _clean_event_text(event.text)
                if 0 < len(clean) <= 3:
                    is_karaoke = True

            if is_karaoke:
                style_stats[style]["karaoke"] += 1

    # Style is karaoke if majority (>50%) of events are karaoke-like
    karaoke_styles = []
    for style, stats in style_stats.items():
        if stats["total"] > 0:
            ratio = stats["karaoke"] / stats["total"]
            if ratio > 0.5:
                karaoke_styles.append(style)
                log.detail(f"  Karaoke detected: {style} "
                           f"({stats['karaoke']}/{stats['total']} = {ratio:.0%})")

    return karaoke_styles


def resolve_styles_with_files(keep_styles: list | None, passthrough_styles: list | None,
                              all_styles: list, files: list) -> tuple:
    """Resolve special markers (ALL, +ALL, +karaoke) using actual file data.

    Args:
        keep_styles: from parse_styles_arg (may contain "ALL")
        passthrough_styles: from parse_styles_arg (may contain "+ALL", "+karaoke")
        all_styles: list of all style names found in files
        files: list of file paths for karaoke detection

    Returns:
        (resolved_keep, resolved_passthrough) - concrete style name lists
    """
    resolved_keep = keep_styles
    resolved_passthrough = []

    # Handle +karaoke
    if passthrough_styles and "+karaoke" in passthrough_styles:
        karaoke = detect_karaoke_styles(files)
        resolved_passthrough.extend(karaoke)
        passthrough_styles = [s for s in passthrough_styles if s != "+karaoke"]
        if karaoke:
            log.info(f"  Auto-detected karaoke: {', '.join(karaoke)}")

    # Add explicit passthrough styles
    if passthrough_styles and "+ALL" in passthrough_styles:
        passthrough_styles = [s for s in passthrough_styles if s != "+ALL"]
        # +ALL means everything not in translate list
        if resolved_keep and resolved_keep != ["ALL"]:
            plus_all = [s for s in all_styles
                        if s not in resolved_keep and s not in resolved_passthrough]
            resolved_passthrough.extend(plus_all)
        # Add any remaining explicit ones
        resolved_passthrough.extend(passthrough_styles)
    elif passthrough_styles:
        resolved_passthrough.extend(passthrough_styles)

    # Handle ALL in keep
    if resolved_keep == ["ALL"]:
        # Translate all except passthrough
        resolved_keep = [s for s in all_styles if s not in resolved_passthrough]

    # Deduplicate
    resolved_passthrough = list(dict.fromkeys(resolved_passthrough))
    if resolved_keep:
        resolved_keep = list(dict.fromkeys(resolved_keep))

    return resolved_keep, resolved_passthrough or None
