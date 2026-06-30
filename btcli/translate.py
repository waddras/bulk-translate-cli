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
from .srt_pre import parse_subtitle_file
from .sub_post import reassemble_files


# ── Auto Style Detection ──────────────────────────────────────────────────────

def _auto_detect_styles(files: list) -> list | None:
    """Auto-detect top N styles by unique line count across all files.

    Uses same cleaning logic as srt_pre (strip tags, normalize newlines)
    so that lines differing only in positioning tags collapse properly.

    Returns list of style names to keep, or None if files are SRT (no styles).
    """
    import pysubs2
    from .srt_pre import _clean_event_text, _should_drop

    top_n = cfg.get("KEEP_TOP_STYLES", 2)
    style_unique_lines: dict = {}  # {style_name: set of cleaned unique texts}
    style_total_lines: dict = {}   # {style_name: total event count}

    for fpath in files:
        try:
            subs = pysubs2.SSAFile.load(str(fpath))
        except Exception:
            continue

        # If no styles (SRT), skip auto-detection
        if not subs.styles or len(subs.styles) <= 1:
            continue

        for event in subs:
            style = getattr(event, "style", "Default")
            if style not in style_unique_lines:
                style_unique_lines[style] = set()
                style_total_lines[style] = 0

            # Clean text same way as srt_pre does
            clean = _clean_event_text(event.text)
            if _should_drop(clean):
                continue

            style_total_lines[style] += 1
            style_unique_lines[style].add(clean)

    if not style_unique_lines:
        return None  # No styles found (SRT files or single style)

    if len(style_unique_lines) <= top_n:
        return None  # Fewer styles than threshold, keep all

    # Sort by unique line count descending, pick top N
    sorted_styles = sorted(style_unique_lines.items(), key=lambda x: len(x[1]), reverse=True)
    kept = [name for name, _ in sorted_styles[:top_n]]

    # Log all styles with counts
    for name, lines in sorted_styles:
        total = style_total_lines.get(name, 0)
        unique = len(lines)
        marker = " ✓" if name in kept else ""
        log.detail(f"  {name}: {total} total, {unique} unique{marker}")

    # Log what was dropped
    dropped = [f"{name} ({len(lines)} unique)" for name, lines in sorted_styles[top_n:]]
    if dropped:
        log.info(f"  Dropped styles: {', '.join(dropped)}")

    return kept


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
    keep_styles: list | None = None,
    passthrough_styles: list | None = None,
    auto_track: bool | None = None,
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
        from .setup_key import check_api_key
        api_key = check_api_key()
        if not api_key:
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

        # Auto track detection
        if auto_track:
            from .auto import auto_select_track_from_files
            detected = auto_select_track_from_files(video_files)
            if detected is not None:
                track_indices = [detected]
            else:
                log.warning("Auto-detect failed, using track 0")
                track_indices = track_indices or [0]
        else:
            track_indices = track_indices or [0]

        tracks = track_indices
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

    # Resolve styles once (applies to all batches)
    log.sep()
    log.phase("STYLE DETECTION")

    if keep_styles is not None or passthrough_styles is not None:
        from .styles import resolve_styles_with_files
        from .srt_pre import get_styles_from_files
        all_styles = get_styles_from_files([str(f) for f in files])
        keep_styles, passthrough_styles = resolve_styles_with_files(
            keep_styles, passthrough_styles, all_styles, [str(f) for f in files]
        )

    if keep_styles is None:
        keep_styles = _auto_detect_styles(files)

    if keep_styles:
        log.info(f"Styles to translate: {', '.join(keep_styles)}")
    if passthrough_styles:
        log.info(f"Passthrough styles: {', '.join(passthrough_styles)}")

    # Detect show name once
    if not show_name:
        show_name = _detect_show_name(files)
    log.stat("Show name", show_name)

    # Batch files — distribute evenly, max FILES_PER_BATCH per batch
    batch_size = cfg.get("FILES_PER_BATCH", 25)
    total_files = len(files)

    if total_files <= batch_size:
        batches = [files]
    else:
        num_batches = (total_files + batch_size - 1) // batch_size
        even_size = (total_files + num_batches - 1) // num_batches
        batches = [files[i:i + even_size] for i in range(0, total_files, even_size)]
        log.info(f"Splitting into {len(batches)} batch(es) of ~{even_size} files")

    # Process each batch
    all_completed = []
    all_warnings = []

    for batch_num, batch_files in enumerate(batches, 1):
        if len(batches) > 1:
            log.sep()
            log.phase(f"BATCH {batch_num}/{len(batches)} — {len(batch_files)} file(s)")

        batch_completed, batch_warnings = _translate_batch(
            batch_files, keep_styles, passthrough_styles,
            show_name, source_lang, target_lang,
            api_key, suffix, force_srt,
        )
        all_completed.extend(batch_completed)
        all_warnings.extend(batch_warnings)

    # Final report
    log.sep()
    log.summary("Translation Complete", [
        ("Files written", str(len(all_completed))),
        ("Warnings", str(len(all_warnings)) if all_warnings else "0"),
        ("Elapsed", log.elapsed()),
    ])

    for f in all_completed:
        log.success(f"  done: {f}")
    for w in all_warnings:
        log.warning(w)


def _translate_batch(
    files: list,
    keep_styles: list | None,
    passthrough_styles: list | None,
    show_name: str,
    source_lang: str,
    target_lang: str,
    api_key: str,
    suffix: str,
    force_srt: bool,
) -> tuple:
    """Translate a single batch of files. Returns (completed, warnings)."""

    # Phase 1: Build blob
    log.sep()
    log.phase("PHASE 1 - Building blob...")
    meta, payload, stats = build_blob(files, keep_styles=keep_styles)
    if stats["total"] == 0:
        log.warning("No dialogue cues found in this batch.")
        return [], []

    max_blob = cfg.get("MAX_BLOB_LINES", 50000)
    if stats["total"] > max_blob:
        log.error(f"Too many cues ({stats['total']} > {max_blob}). Reduce FILES_PER_BATCH.")
        return [], [f"Batch exceeded MAX_BLOB_LINES ({stats['total']} > {max_blob})"]

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
        translated_blob, meta, files, suffix=suffix, force_srt=force_srt,
        kept_styles=keep_styles, passthrough_styles=passthrough_styles,
    )

    # Batch report
    total_keys = len(set().union(*[set(ch.keys()) for ch in chunks]))
    translated_count = len(translated_unique)
    pct = round(translated_count / total_keys * 100) if total_keys else 0
    log.info(f"  Batch: {translated_count}/{total_keys} unique lines ({pct}%)")

    return completed, warnings
