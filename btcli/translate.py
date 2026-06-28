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
from .logger import log
from .sub_post import reassemble_files


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
        log.error("No API key. Set GEMINI_API_KEY in settings.conf or environment.")
        return

    log.sep()
    log.phase(f"TRANSLATE - {source_lang} → {target_lang}")
    log.stat("Path", path)
    log.stat("Input", f"{input_type} | Suffix: {suffix} | Force SRT: {force_srt}")
    log.sep()

    # Phase 0: Discover/Extract files
    if input_type == "vid":
        video_files = discover_files(path, mode="vid", scan_mode="recursive",
                                     filter_pattern=filter_pattern)
        if not video_files:
            log.error(f"No video files found in: {path}")
            return

        log.info(f"Found {len(video_files)} video file(s)")
        tracks = track_indices or [0]
        log.info(f"Extracting track(s): {tracks}")
        log.sep()

        # Extract with source language suffix so the file is kept
        source_suffix = get_suffix_for_lang(source_lang)
        sub_files = extract_from_videos(
            [str(f) for f in video_files],
            tracks,
            suffix=source_suffix,
            force_srt=force_srt,
        )
        if not sub_files:
            log.error("No subtitles extracted.")
            return

        files = [Path(f) for f in sub_files]
    else:
        files = discover_files(path, mode="sub", scan_mode="recursive",
                               filter_pattern=filter_pattern)
        if not files:
            log.error(f"No subtitle files found in: {path}")
            if filter_pattern:
                log.detail(f"  (filter: '{filter_pattern}')")
            return

    # Report files
    log.sep()
    log.phase(f"FILES - {len(files)} subtitle file(s)")
    for i, f in enumerate(files, 1):
        f = Path(f)
        log.item(f"[{i:02d}] {f.name}  ({f.stat().st_size / 1024:.1f} KB)")

    # Phase 1: Build blob
    log.sep()
    log.phase("PHASE 1 - Building blob...")
    meta, payload, stats = build_blob(files)
    if stats["total"] == 0:
        log.error("No dialogue cues found in selected files.")
        return

    max_blob = cfg.get("MAX_BLOB_LINES", 50000)
    if stats["total"] > max_blob:
        log.error(f"Too many cues ({stats['total']} > {max_blob}). Select fewer files.")
        return

    log.info(f"DEDUP: {stats['total']} total → {stats['unique']} unique "
             f"({stats['collapsed']} collapsed, ~{stats['pct']}% fewer tokens)")

    # Phase 2: Split into chunks
    log.sep()
    log.phase("PHASE 2 - Splitting into chunks...")
    chunks = split_blob(payload)
    log.info(f"Split into {len(chunks)} chunk(s)")
    total_tokens = 0
    for i, ch in enumerate(chunks, 1):
        est = estimate_output_tokens(ch)
        total_tokens += est
        log.detail(f"  Chunk {i}: {len(ch)} lines, ~{est} output tokens")
    log.stat("Total estimated output tokens", str(total_tokens))

    # Detect show name
    if not show_name:
        show_name = _detect_show_name(files)
    log.stat("Show name", show_name)

    mode = cfg.get("TRANSLATION_MODE", "chunked")
    log.stat("Translation mode", mode)

    # Phase 3: Translate
    log.sep()
    log.phase("PHASE 3 - Translating...")
    log.start_progress("Translating", total=len(chunks))

    translated_unique = asyncio.run(
        run_translation(chunks, payload, api_key, show_name, source_lang, target_lang)
    )

    log.finish_progress()

    # Phase 4: Reassemble
    log.sep()
    log.phase("PHASE 4 - Reassembling output files...")
    translated_blob = expand_translations(translated_unique, meta)
    completed, warnings = reassemble_files(
        translated_blob, meta, files, suffix=suffix, force_srt=force_srt
    )

    # Report
    log.sep()
    total_keys = len(set().union(*[set(ch.keys()) for ch in chunks]))
    translated_count = len(translated_unique)
    pct = round(translated_count / total_keys * 100) if total_keys else 0

    log.summary("Translation Complete", [
        ("Files written", str(len(completed))),
        ("Translated", f"{translated_count}/{total_keys} unique lines ({pct}%)"),
        ("Warnings", str(len(warnings)) if warnings else "0"),
        ("Elapsed", log.elapsed()),
    ])

    for f in completed:
        log.success(f"  done: {f}")
    for w in warnings:
        log.warning(w)
