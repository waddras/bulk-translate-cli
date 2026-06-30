"""Auto-detection: pick best track and styles automatically.

Track selection logic:
  1. Skip bitmap tracks (PGS, DVD, DVB)
  2. Prefer tracks with language matching SOURCE_LANGUAGE
  3. Prefer "Full" in title over "Signs"/"Songs"
  4. Prefer "without honorifics" over "with honorifics"
  5. If no language tag, assume English if text tracks exist

Style selection default in auto mode:
  -s "ALL,+karaoke"
"""
from __future__ import annotations

from .config import cfg
from .extract import probe_tracks, _BITMAP_CODECS
from .logger import log


# ── Track Title Keywords ──────────────────────────────────────────────────────

# Titles that indicate non-dialogue tracks (skip these)
_SKIP_KEYWORDS = ["signs", "songs", "signs and songs", "signs/songs"]

# Titles that indicate full dialogue (prefer these)
_PREFER_KEYWORDS = ["full"]

# Prefer tracks without honorifics
_DEPRIORITIZE_KEYWORDS = ["with honorifics", "honorifics"]
_PRIORITIZE_KEYWORDS = ["without honorifics"]


def auto_select_track(filepath: str) -> int | None:
    """Auto-select the best subtitle track for translation.

    Args:
        filepath: path to video file

    Returns:
        Track index (0-based) or None if no suitable track found.
    """
    tracks = probe_tracks(filepath)
    if not tracks:
        return None

    source_lang = cfg.get("SOURCE_LANGUAGE", "english").lower()

    # Filter to text-based tracks only
    text_tracks = [t for t in tracks if t["codec"] not in _BITMAP_CODECS]
    if not text_tracks:
        return None

    # Score each track
    scored = []
    for t in text_tracks:
        score = 0
        title = (t.get("title") or "").lower()
        lang = (t.get("language") or "").lower()

        # Language match from metadata
        if source_lang in lang or source_lang[:3] in lang or source_lang[:2] in lang:
            score += 100
        elif lang == "eng" or lang == "en":
            if source_lang == "english":
                score += 100
        elif lang == "und" or not lang:
            # Unknown language — slight penalty but don't skip
            score += 10

        # Title-based scoring
        title_lower = title.lower()

        # Skip signs/songs only tracks
        is_signs_only = any(kw in title_lower for kw in _SKIP_KEYWORDS)
        if is_signs_only and "full" not in title_lower:
            score -= 50

        # Prefer "full" in title
        if any(kw in title_lower for kw in _PREFER_KEYWORDS):
            score += 30

        # Prefer "without honorifics"
        if any(kw in title_lower for kw in _PRIORITIZE_KEYWORDS):
            score += 20
        elif any(kw in title_lower for kw in _DEPRIORITIZE_KEYWORDS):
            score -= 10

        # Prefer ASS over SRT (more features)
        if t["codec"] in ("ass", "ssa"):
            score += 5

        scored.append((score, t["index"], t))

    if not scored:
        return None

    # Sort by score descending, pick best
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_idx, best_track = scored[0]

    title = best_track.get("title") or f"track {best_idx}"
    log.detail(f"  Auto-selected track {best_idx}: [{best_track.get('language', 'und')}] "
               f"({best_track['codec']}) \"{title}\" (score: {best_score})")

    return best_idx


def auto_select_track_from_files(video_files: list) -> int | None:
    """Auto-select track from the first video file (assumes all have same layout).

    Returns track index or None.
    """
    if not video_files:
        return None

    # Use first file to determine track
    result = auto_select_track(str(video_files[0]))
    if result is not None:
        log.info(f"  Auto-detected track: {result}")
    else:
        log.warning("  Could not auto-detect a suitable text track")
    return result
