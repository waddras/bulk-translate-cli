"""Subtitle parsing: load SRT/ASS files, clean text, preserve tags, scan tags.

Responsibilities:
  - Parse SRT/ASS via pysubs2
  - Strip all ASS override tags except those in PRESERVE_TAGS
  - Clean text (newline normalization, noise removal)
  - Scan for unique tag names (for probe -o tags)
  - Style detection
"""
from __future__ import annotations

import re
from pathlib import Path

import pysubs2

from .config import cfg

# ── Regex ─────────────────────────────────────────────────────────────────────
_ALL_TAGS_RE = re.compile(r"\{[^}]*\}")
_TAG_NAME_RE = re.compile(r"\\([a-zA-Z]+)")


# ── Tag Handling ──────────────────────────────────────────────────────────────

def _get_preserve_tags() -> list:
    """Parse PRESERVE_TAGS config into a list of tag names."""
    raw = cfg.get("PRESERVE_TAGS", "pos, an, move, fad, fade;")
    raw = raw.rstrip(";").strip()
    return [t.strip() for t in raw.split(",") if t.strip()]


def _extract_pos_tags(raw: str) -> tuple:
    """Extract preserved ASS tags from raw event text.

    Returns (pos_tags_string, cleaned_text).
    pos_tags_string: e.g. "{\\pos(320,50)\\an8}" or ""
    cleaned_text: text with all tags removed
    """
    preserve_list = _get_preserve_tags()
    if not preserve_list:
        clean = _ALL_TAGS_RE.sub("", raw)
        clean = clean.replace(r"\N", "\n").replace(r"\n", "\n").strip()
        return "", clean

    tag_pattern = "|".join(re.escape(t) for t in preserve_list)
    preserve_re = re.compile(r"\\(?:" + tag_pattern + r")(?:\([^)]*\)|[^\\}]*)")

    preserved = []
    for match in re.finditer(r"\{([^}]*)\}", raw):
        block_content = match.group(1)
        found = preserve_re.findall(block_content)
        if found:
            preserved.extend(found)

    pos_string = "{" + "".join(preserved) + "}" if preserved else ""
    clean = _ALL_TAGS_RE.sub("", raw)
    clean = clean.replace(r"\N", "\n").replace(r"\n", "\n").strip()
    return pos_string, clean


def _clean_event_text(raw: str) -> str:
    """Remove all tags and normalize newlines."""
    text = _ALL_TAGS_RE.sub("", raw or "")
    text = text.replace(r"\N", "\n").replace(r"\n", "\n")
    return text.strip()


def _should_drop(text: str) -> bool:
    """Drop empty or single-char noise lines."""
    if not text:
        return True
    if len(text) == 1 and not text.isdigit():
        return True
    return False


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_subtitle_file(path, keep_styles: list | None = None) -> list:
    """Parse SRT/ASS file → list of {text, start, end, pos_tags}.

    Args:
        path: Path to subtitle file
        keep_styles: if provided, only keep events with these style names

    Returns:
        List of cue dicts: [{text, start, end, pos_tags}, ...]
    """
    subs = pysubs2.SSAFile.load(str(path))
    cues = []
    preserve_tags = _get_preserve_tags()

    for event in subs:
        # Style filtering
        if keep_styles is not None and hasattr(event, "style"):
            if event.style not in keep_styles:
                continue

        # Tag preservation
        if preserve_tags and event.text:
            pos_tags, clean = _extract_pos_tags(event.text)
        else:
            pos_tags = ""
            clean = _clean_event_text(event.text)

        if _should_drop(clean):
            continue

        cues.append({
            "text": clean,
            "start": event.start,
            "end": event.end,
            "pos_tags": pos_tags,
            "style": getattr(event, "style", "Default"),
        })

    return cues


# ── Style Detection ───────────────────────────────────────────────────────────

def get_styles_from_file(path) -> list:
    """Return ASS style names from a single file."""
    try:
        subs = pysubs2.SSAFile.load(str(path))
        return sorted(subs.styles.keys())
    except Exception:
        return []


def get_styles_from_files(paths: list) -> list:
    """Return unique ASS style names across all files."""
    all_styles = set()
    for p in paths:
        all_styles.update(get_styles_from_file(p))
    return sorted(all_styles)


# ── Tag Scanning ──────────────────────────────────────────────────────────────

def scan_tags_from_file(path) -> set:
    """Scan a subtitle file for unique ASS override tag names.

    Returns set of tag names like {'an', 'pos', 'fscx', 'move', 'b', 'i'}.
    """
    tags = set()
    try:
        subs = pysubs2.SSAFile.load(str(path))
        for event in subs:
            if event.text:
                for match in re.finditer(r"\{([^}]*)\}", event.text):
                    block = match.group(1)
                    found = _TAG_NAME_RE.findall(block)
                    tags.update(found)
    except Exception:
        pass
    return tags


def scan_tags_from_files(paths: list) -> list:
    """Scan multiple files for unique ASS tag names. Returns sorted list."""
    all_tags = set()
    for p in paths:
        all_tags.update(scan_tags_from_file(p))
    return sorted(all_tags)
