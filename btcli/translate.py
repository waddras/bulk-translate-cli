"""Translate flow orchestrator: ties the full pipeline together.

Pipeline:
  1. Discover files (or receive pre-resolved list)
  2. Extract tracks if input is video
  3. Parse subtitles → build deduped blob
  4. Split into chunks
  5. Translate via Gemini API (mode from config)
  6. Reassemble output files
  7. Report results
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from pathlib import Path

from .ai import run_translation
from .blob import build_blob, expand_translations, estimate_output_tokens, split_blob
from .config import cfg, get_lang_code, get_suffix_for_lang
from .discover import discover_files
from .extract import extract_from_videos
from .sub_post import reassemble_files

SEP = "=" * 60


# ── Show Name Detection ───────────────────────────────────────────────────────

def _detect_show_name(files: list) -> str:
    """Auto-detect show name from filenames (prefix before ' - S' pattern)."""
    names = [Path(f).stem for f in files]
    if not names:
        return ""

    patterns = [r'^(.+?)\s*-\s*S\d', r'^(.+?)\s*-\s*E\d', r'^(.+?)\s+S\d']
    for pattern in patterns:
        matches = []
        for name in names:
            m = re.match(pattern, name)
            if m:
                matches.append(m.group(1).strip())
        if matches:
            return Counter(matches).most_common(1)[0][0]

    # Single file: split on ' - '
    if len(names) == 1:
        return names[0].split(' - ')[0].strip() if ' - ' in names[0] else names[0]

    # Multiple files: common prefix
    prefix = names[0]
    for name in names[1:]:
        while not name.startswith(prefix) and prefix:
            prefix = prefix[:-1]
    return prefix.strip().rstrip('-').strip()


# ── Language Parsing ──────────────────────────────────────────────────────────

def _parse_language(lang_arg: str) -> tuple:
    """Parse language argument into (source_lang, target_lang).

    Accepts:
      - "arabic" → ("english", "arabic")
      - "japanese,english" → ("japanese", "english")
    """
    if "," in lang_arg:
        parts = [p.strip().lower() for p in lang_arg.split(",", 1)]
        return parts[0], parts[1]
    return cfg.get("SOURCE_LANGUAGE", "english"), lang_arg.strip().lower()


# ── Main Translate Runner ─────────────────────────────────────────────────────

def run_translate(
    path: str,
    lang: str = "arabic",
    input_type: str = "sub",
    filter_pattern: str | None = None,
    track_indices: list | None = None,
    suffix: str | None = None,
    force_srt: bool = False,
    show_name: str = "",
) -> None:
    """Run the full translation pipeline.

    Args:
        path: file or directory path
        lang: target language or "source,target" pair
        input_type: "vid" or "sub"
        filter_pattern: filename filter
        track_indices: track index(es) for video extraction
        suffix: output suffix override (e.g. ".ar")
        force_srt: force SRT output
        show_name: override auto-detected show name
    """
    # Parse language
    source_lang, target_lang = _parse_language(lang)
    if suffix is None:
        suffix = get_suffix_for_lang(target_lang)

    # API key check
    api_key = cfg.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: No API key. Set GEMINI_API_KEY in settings.conf or environment.")
        return

    print(SEP)
    print(f"TRANSLATE - {source_lang} → {target_lang}")
    print(f"  Path: {path}")
    print(f"  Input: {input_type} | Suffix: {suffix} | Force SRT: {force_srt}")
    print(SEP)

    # Phase 0: Discover/Extract files
    if input_type == "vid":
        # Discover video files (always recursive for translate)
        video_files = discover_files(path, mode="vid", scan_mode="recursive",
                                     filter_pattern=filter_pattern)
        if not video_files:
            print(f"ERROR: No video files found in: {path}")
            return

        print(f"Found {len(video_files)} video file(s)")
        tracks = track_indices or [0]
        print(f"Extracting track(s): {tracks}")
        print(SEP)

        # Extract subtitle tracks
        sub_files = extract_from_videos(
            [str(f) for f in video_files],
            tracks,
            force_srt=force_srt,
        )
        if not sub_files:
            print("ERROR: No subtitles extracted.")
            return

        files = [Path(f) for f in sub_files]
    else:
        # Discover subtitle files (always recursive for translate)
        files = discover_files(path, mode="sub", scan_mode="recursive",
                               filter_pattern=filter_pattern)
        if not files:
            print(f"ERROR: No subtitle files found in: {path}")
            if filter_pattern:
                print(f"  (filter: '{filter_pattern}')")
            return

    # Report files
    print(SEP)
    print(f"TRANSLATE - {len(files)} subtitle file(s)")
    for i, f in enumerate(files, 1):
        f = Path(f)
        print(f"  [{i:02d}] {f.name}  ({f.stat().st_size / 1024:.1f} KB)")

    # Phase 1: Build blob
    print(SEP)
    print("PHASE 1 - Building blob...")
    meta, payload, stats = build_blob(files)
    if stats["total"] == 0:
        print("ERROR: No dialogue cues found in selected files.")
        return

    max_blob = cfg.get("MAX_BLOB_LINES", 50000)
    if stats["total"] > max_blob:
        print(f"ERROR: Too many cues ({stats['total']} > {max_blob}). Select fewer files.")
        return

    print(f"DEDUP: {stats['total']} total -> {stats['unique']} unique "
          f"({stats['collapsed']} collapsed, ~{stats['pct']}% fewer tokens)")

    # Phase 2: Split into chunks
    print(SEP)
    print("PHASE 2 - Splitting into chunks...")
    chunks = split_blob(payload)
    print(f"Split into {len(chunks)} chunk(s)")
    total_tokens = 0
    for i, ch in enumerate(chunks, 1):
        est = estimate_output_tokens(ch)
        total_tokens += est
        print(f"  Chunk {i}: {len(ch)} lines, ~{est} output tokens")
    print(f"Total estimated output tokens: {total_tokens}")

    # Detect show name
    if not show_name:
        show_name = _detect_show_name(files)
    print(f"Show name: {show_name}")

    mode = cfg.get("TRANSLATION_MODE", "chunked")
    print(f"Translation mode: {mode}")

    # Phase 3: Translate
    print(SEP)
    print("PHASE 3 - Translating...")
    translated_unique = asyncio.run(
        run_translation(chunks, payload, api_key, show_name, source_lang, target_lang)
    )

    # Phase 4: Reassemble
    print(SEP)
    print("PHASE 4 - Reassembling output files...")
    translated_blob = expand_translations(translated_unique, meta)
    completed, warnings = reassemble_files(
        translated_blob, meta, files, suffix=suffix, force_srt=force_srt
    )

    # Report
    print(SEP)
    total_keys = len(set().union(*[set(ch.keys()) for ch in chunks]))
    translated_count = len(translated_unique)
    print(f"COMPLETE - {len(completed)} files written")
    print(f"  Translated: {translated_count}/{total_keys} unique lines "
          f"({round(translated_count / total_keys * 100) if total_keys else 0}%)")
    if warnings:
        print(f"  Warnings: {len(warnings)}")
    for f in completed:
        print(f"  done: {f}")
    for w in warnings:
        print(f"  warning: {w}")
