"""Subtitle parsing: load SRT/ASS files, clean text, preserve tags, scan tags.

Responsibilities:
  - Parse SRT/ASS via pysubs2
  - Strip blacklisted ASS override tags (STRIP_TAGS), keep the rest
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

def _get_strip_tags() -> list:
    """Parse STRIP_TAGS config into a list of tag names to remove."""
    raw = cfg.get("STRIP_TAGS", "fn, fs, b, i, u, s;")
    raw = raw.rstrip(";").strip()
    return [t.strip() for t in raw.split(",") if t.strip()]


def _extract_tags(raw: str) -> tuple:
    """Extract all ASS tags except blacklisted ones from raw event text.

    Keeps all tags that are NOT in STRIP_TAGS.

    Returns (kept_tags_string, cleaned_text).
    kept_tags_string: e.g. "{\\pos(320,50)\\an8\\blur1.5}" or ""
    cleaned_text: text with all tag blocks removed
    """
    strip_list = _get_strip_tags()
    if not strip_list:
        # Nothing to strip — keep all tags
        return "", raw.replace(r"\N", "\n").replace(r"\n", "\n").strip()

    # Build regex for each strip tag with appropriate boundaries
    # \fn always takes text (font name) — match until next \ or }
    # \fs matches only when followed by digit (not fscx/fscy)
    # \b matches only when followed by digit or } (not blur/bord)
    # \i, \u, \s match only when followed by digit or } or \
    patterns = []
    for t in strip_list:
        if t == "fn":
            # \fn followed by anything until next \ or }
            patterns.append(r"\\fn[^\\}]*")
        elif t == "fs":
            # \fs followed by digit (font size), not fscx/fscy
            patterns.append(r"\\fs\d[^\\}]*")
        elif t == "b":
            # \b followed by digit (bold), not blur/bord
            patterns.append(r"\\b\d[^\\}]*")
        elif t == "i":
            # \i followed by digit or end
            patterns.append(r"\\i[01]?(?=[\\}]|$)")
        elif t == "u":
            patterns.append(r"\\u[01]?(?=[\\}]|$)")
        elif t == "s":
            # \s followed by digit (strikeout), not shad
            patterns.append(r"\\s\d[^\\}]*")
        else:
            # Generic: exact match with no trailing letters
            patterns.append(r"\\" + re.escape(t) + r"(?![a-zA-Z])(?:\([^)]*\)|[^\\}]*)")

    strip_re = re.compile("|".join(patterns))

    kept = []
    for match in re.finditer(r"\{([^}]*)\}", raw):
        block_content = match.group(1)
        # Remove stripped tags, keep the rest
        remaining = strip_re.sub("", block_content).strip()
        if remaining:
            kept.append(remaining)

    kept_string = "{" + "".join(kept) + "}" if kept else ""
    clean = _ALL_TAGS_RE.sub("", raw)
    clean = clean.replace(r"\N", "\n").replace(r"\n", "\n").strip()
    return kept_string, clean


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
    strip_tags = _get_strip_tags()

    for event in subs:
        # Style filtering
        if keep_styles is not None and hasattr(event, "style"):
            if event.style not in keep_styles:
                continue

        # Tag handling: strip blacklisted tags, keep the rest
        if strip_tags and event.text:
            kept_tags, clean = _extract_tags(event.text)
        else:
            kept_tags = ""
            clean = _clean_event_text(event.text)

        if _should_drop(clean):
            continue

        cues.append({
            "text": clean,
            "start": event.start,
            "end": event.end,
            "pos_tags": kept_tags,
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
