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
from .blob import (
    build_blob,
    expand_translations,
    estimate_output_tokens,
    split_blob,
    split_blob_by_files,
)
from .config import cfg, get_lang_code, get_suffix_for_lang
from .discover import discover_files, exclude_translated_output
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
    force: bool = False,
    files_per_call: int | None = None,
    preset_files: list | None = None,
    use_cache: bool = True,
) -> dict | None:
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
        preset_files: explicit input files, skipping discovery. Interactive mode
            uses this so a per-directory run cannot pick up a sibling
            directory's files.
    """
    # Parse language
    source_lang, target_lang = _parse_language(lang)
    if suffix is None:
        suffix = get_suffix_for_lang(target_lang)
    if files_per_call is not None and files_per_call < 1:
        log.error("--files-per-call must be at least 1")
        return

    from .manifest import ManifestRun
    manifest_run = ManifestRun({
        "path": str(Path(path).resolve()),
        "input_type": input_type,
        "source_language": source_lang,
        "target_language": target_lang,
        "suffix": suffix,
        "tracks": track_indices or [0],
        "force_srt": force_srt,
        "force_extraction": force,
        "files_per_call": files_per_call,
        "model": cfg.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        "translation_mode": cfg.get("TRANSLATION_MODE", "chunked"),
        "max_lines_per_chunk": cfg.get("MAX_LINES_PER_CHUNK", 1000),
        "parallel_chunks": cfg.get("PARALLEL_CHUNKS", 1),
    })

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
        video_files = [Path(f) for f in preset_files] if preset_files else discover_files(
            path, mode="vid", scan_mode="recursive", filter_pattern=filter_pattern)
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

        if not show_name:
            show_name = _detect_show_name(video_files)
        manifest_run.register_files(video_files, series=show_name)
        manifest_run.update_command(show_name=show_name, tracks=tracks)

        # Extract with source language suffix so the file is kept. A previous
        # manifest record is reusable only when its file still exists.
        from .manifest import find_reusable_extraction
        source_suffix = get_suffix_for_lang(source_lang)
        sub_files = extract_from_videos(
            [str(f) for f in video_files],
            tracks,
            suffix=source_suffix,
            force_srt=force_srt,
            force=force,
            reuse_lookup=find_reusable_extraction,
            extraction_callback=lambda video, valid_tracks, extracted, codec, reused: (
                manifest_run.record_extraction(video, valid_tracks, extracted, codec, reused)
            ),
        )
        if not sub_files:
            log.error("No subtitles extracted.")
            manifest_run.finish()
            return

        files = [Path(f) for f in sub_files]
    else:
        files = [Path(f) for f in preset_files] if preset_files else discover_files(
            path, mode="sub", scan_mode="recursive", filter_pattern=filter_pattern)
        if not files:
            log.error(f"No subtitle files found in: {path}")
            if filter_pattern:
                log.detail(f"  (filter: '{filter_pattern}')")
            return

        # Never treat this tool's own output as a source.
        files, prior_output = exclude_translated_output(files, suffix)
        if prior_output:
            log.info(f"Skipping {len(prior_output)} previously translated "
                     f"file(s) ending in '{suffix}'")
            for item in prior_output[:5]:
                log.detail(f"  skipped: {Path(item).name}")
        if not files:
            log.error(f"Only previously translated files found in: {path}")
            return

    if not show_name:
        show_name = _detect_show_name(files)
    manifest_run.register_files(files, series=show_name)
    manifest_run.update_command(show_name=show_name)

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

    manifest_run.update_command(
        styles_to_translate=keep_styles or [],
        passthrough_styles=passthrough_styles or [],
    )

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

    all_missing = set()

    try:
        for batch_num, batch_files in enumerate(batches, 1):
            if len(batches) > 1:
                log.sep()
                log.phase(f"BATCH {batch_num}/{len(batches)} — {len(batch_files)} file(s)")

            batch_completed, batch_warnings, batch_missing = _translate_batch(
                batch_files, keep_styles, passthrough_styles,
                show_name, source_lang, target_lang,
                api_key, suffix, force_srt,
                files_per_call=files_per_call,
                manifest_run=manifest_run,
                use_cache=use_cache,
            )
            all_completed.extend(batch_completed)
            all_warnings.extend(batch_warnings)
            all_missing |= batch_missing
    finally:
        manifest_run.finish()

    # Final report
    log.sep()
    log.summary("Translation Complete", [
        ("Files written", str(len(all_completed))),
        ("Warnings", str(len(all_warnings)) if all_warnings else "0"),
        ("Lines missing", str(len(all_missing)) if all_missing else "0"),
        ("Elapsed", log.elapsed()),
    ])

    for f in all_completed:
        log.success(f"  done: {f}")
    for w in all_warnings:
        log.warning(w)

    return {
        "completed": all_completed,
        "warnings": all_warnings,
        "missing": all_missing,
        "files": files,
        "path": path,
        "lang": f"{source_lang},{target_lang}",
        "keep_styles": keep_styles,
        "passthrough_styles": passthrough_styles,
        "show_name": show_name,
        "suffix": suffix,
        "force_srt": force_srt,
    }


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
    files_per_call: int | None = None,
    manifest_run=None,
    use_cache: bool = True,
) -> tuple:
    """Translate one batch, writing each file as soon as its lines are ready.

    Returns (completed, warnings, still_missing_keys).
    """

    def _record_early_failure(status: str, reason: str) -> None:
        if not manifest_run:
            return
        for source in files:
            manifest_run.record_translation(Path(source), {
                "output": None,
                "source_language": source_lang,
                "target_language": target_lang,
                "suffix": suffix,
                "model": cfg.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
                "mode": cfg.get("TRANSLATION_MODE", "chunked"),
                "files_per_call": files_per_call,
                "status": status,
                "reason": reason,
                "elapsed": log.elapsed(),
            })

    # Phase 1: Build blob
    log.sep()
    log.phase("PHASE 1 - Building blob...")
    meta, payload, stats = build_blob(files, keep_styles=keep_styles)
    if stats["total"] == 0:
        log.warning("No dialogue cues found in this batch.")
        _record_early_failure("no_dialogue", "No dialogue cues matched the selected styles")
        return [], [], set()

    max_blob = cfg.get("MAX_BLOB_LINES", 50000)
    if stats["total"] > max_blob:
        log.error(f"Too many cues ({stats['total']} > {max_blob}). Reduce FILES_PER_BATCH.")
        reason = f"Batch exceeded MAX_BLOB_LINES ({stats['total']} > {max_blob})"
        _record_early_failure("failed", reason)
        return [], [reason], set()

    log.info(f"DEDUP: {stats['total']} total → {stats['unique']} unique "
             f"({stats['collapsed']} collapsed, ~{stats['pct']}% fewer tokens)")

    # Cache: lines already translated in an earlier run are served from disk,
    # so only what is genuinely missing is sent to the API.
    cache = None
    cached_hits: dict = {}
    to_translate = payload
    if use_cache and cfg.get("USE_TRANSLATION_CACHE", True):
        from .cache import TranslationCache, series_root_for
        cache = TranslationCache(series_root_for(files), target_lang)
        cached_hits, to_translate = cache.split(payload)
        if cached_hits:
            log.info(f"CACHE: {len(cached_hits)} line(s) already translated, "
                     f"{len(to_translate)} still to translate")
            log.detail(f"  Cache file: {cache.path}")
        elif cache.loaded:
            log.detail(f"  Cache has {cache.loaded} entry(ies), none matched this batch")

    if not to_translate:
        log.success("Every line was already translated - nothing to send to the API")

    # Phase 2: Split into line chunks or explicit whole-file calls.
    log.sep()
    log.phase("PHASE 2 - Splitting into chunks...")
    if not to_translate:
        chunks = []
        log.info("No chunks needed")
    elif files_per_call:
        chunks = split_blob_by_files(to_translate, meta, files_per_call)
        log.info(f"Split into {len(chunks)} call(s), up to {files_per_call} whole file(s) per call")
        log.info("MAX_LINES_PER_CHUNK bypassed for this run")
    else:
        chunks = split_blob(to_translate)
        log.info(f"Split into {len(chunks)} chunk(s)")

    total_tokens = 0
    for i, chunk in enumerate(chunks, 1):
        estimate = estimate_output_tokens(chunk)
        total_tokens += estimate
        log.detail(f"  Chunk {i}: {len(chunk)} lines, ~{estimate} output tokens")
    log.stat("Chunk sizes", ", ".join(str(len(chunk)) for chunk in chunks) or "none")
    log.stat("Total estimated output tokens", str(total_tokens))

    mode = cfg.get("TRANSLATION_MODE", "chunked")
    log.stat("Translation mode", mode)

    # Build per-file completion requirements. A file is safe to write only when
    # every representative key needed by all of its cues has a translation.
    cues_by_file = {index: [] for index in range(1, len(files) + 1)}
    for tag, item in meta.items():
        cues_by_file[item["file_idx"]].append((tag, item))
    required_by_file = {
        index: {item["rep"] for _, item in cues}
        for index, cues in cues_by_file.items()
    }
    chunk_keys = [set(chunk) for chunk in chunks]
    emitted = set()
    completed = []
    warnings = []

    def _translation_details(file_idx: int, translated: dict, status: str,
                             output: str | None = None) -> dict:
        cues = cues_by_file[file_idx]
        required = required_by_file[file_idx]
        translated_cues = sum(1 for _, item in cues if item["rep"] in translated)
        file_chunk_sizes = [len(required & keys) for keys in chunk_keys]
        return {
            "output": output,
            "source_language": source_lang,
            "target_language": target_lang,
            "suffix": suffix,
            "model": cfg.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            "model_pool": cfg.get("MODEL_POOL", []),
            "mode": mode,
            "files_per_call": files_per_call,
            "max_lines_per_chunk": None if files_per_call else cfg.get("MAX_LINES_PER_CHUNK", 1000),
            "batch_chunk_sizes": [len(chunk) for chunk in chunks],
            "file_lines_per_chunk": [size for size in file_chunk_sizes if size],
            "cues": len(cues),
            "unique_lines": len(required),
            "deduplicated_lines": len(cues) - len(required),
            "translated": translated_cues,
            "total": len(cues),
            "missing_unique": len(required - set(translated)),
            "styles_to_translate": keep_styles or [],
            "passthrough_styles": passthrough_styles or [],
            "status": status,
            "elapsed": log.elapsed(),
        }

    def _emit(file_idx: int, translated: dict, status: str,
              missing_keys: set | None = None) -> bool:
        """Write one output file and record it. Returns True when written."""
        source = Path(files[file_idx - 1])
        try:
            translated_blob = expand_translations(translated, meta)
            written, file_warnings = reassemble_files(
                translated_blob, meta, files,
                suffix=suffix, force_srt=force_srt,
                kept_styles=keep_styles, passthrough_styles=passthrough_styles,
                only_file_indices={file_idx},
            )
        except Exception as exc:
            log.detail(f"    Could not write {source.name}: {exc}")
            return False

        if not written:
            return False

        emitted.add(file_idx)
        completed.extend(written)
        warnings.extend(file_warnings)

        if manifest_run:
            details = _translation_details(file_idx, translated, status, written[0])
            if missing_keys:
                details["untranslated_lines"] = [
                    payload[key] for key in sorted(missing_keys) if key in payload
                ][:50]
            manifest_run.record_translation(source, details)
        return True

    def _write_ready_files(translated: dict) -> None:
        """Write every file whose lines are now all translated."""
        translated_keys = set(translated)
        for file_idx in range(1, len(files) + 1):
            if file_idx in emitted:
                continue
            required = required_by_file[file_idx]
            if not required or not required.issubset(translated_keys):
                continue
            source = Path(files[file_idx - 1])
            log.info(f"  All lines ready: {source.name} — generating output now")
            _emit(file_idx, translated, "complete")

    # Phase 3: Translate. After each response the new lines are written to the
    # cache first, then any file that is now complete is generated, so an
    # interrupted run never loses translated lines.
    def _on_progress(fresh: dict) -> None:
        if cache is not None:
            cache.store_and_flush(to_translate, fresh)
        _write_ready_files({**cached_hits, **fresh})

    log.sep()
    log.phase("PHASE 3 - Translating and writing completed files...")

    fresh_translations: dict = {}
    if chunks:
        log.start_progress("Translating", total=len(chunks))
        try:
            fresh_translations = asyncio.run(
                run_translation(
                    chunks, payload, api_key, show_name, source_lang, target_lang,
                    progress_callback=_on_progress,
                )
            )
        finally:
            # Always tear the live display down, even on error or Ctrl-C.
            log.finish_progress()
            if cache is not None:
                cache.store_and_flush(to_translate, fresh_translations)

    translated_unique = {**cached_hits, **fresh_translations}

    # Final readiness pass and explicit handling for incomplete files. No file
    # is generated with shifted, missing, or source-language fallback lines.
    log.sep()
    log.phase("PHASE 4 - Finalizing file completion...")
    _write_ready_files(translated_unique)

    # Files still short a few lines are written anyway, up to the configured
    # tolerance, with those lines left in the source language and reported so a
    # later resume can finish them.
    tolerance = max(0, cfg.get("PARTIAL_LINE_TOLERANCE", 10))
    still_missing: set = set()

    for file_idx in range(1, len(files) + 1):
        if file_idx in emitted:
            continue
        source = Path(files[file_idx - 1])
        missing_keys = required_by_file[file_idx] - set(translated_unique)
        still_missing |= missing_keys

        if missing_keys and len(missing_keys) <= tolerance:
            log.info(f"  {source.name}: {len(missing_keys)} line(s) missing "
                     f"(within tolerance of {tolerance}) — writing anyway")
            for key in sorted(missing_keys):
                log.detail(f"      untranslated: {payload.get(key, '')[:80]}")
            if _emit(file_idx, translated_unique, "partial", missing_keys):
                message = (f"{source.name}: written with {len(missing_keys)} line(s) "
                           f"left in {source_lang}")
                log.warning(message)
                warnings.append(message)
                continue

        message = (f"{source.name}: not written because {len(missing_keys)} "
                   f"unique line(s) remain untranslated")
        log.warning(message)
        warnings.append(message)
        if manifest_run:
            details = _translation_details(file_idx, translated_unique, "incomplete")
            details["untranslated_lines"] = [
                payload[key] for key in sorted(missing_keys) if key in payload
            ][:50]
            manifest_run.record_translation(source, details)

    total_keys = len(payload)
    translated_count = len(set(payload) & set(translated_unique))
    pct = round(translated_count / total_keys * 100) if total_keys else 0
    log.info(f"  Batch: {translated_count}/{total_keys} unique lines ({pct}%)")
    log.info(f"  Files written: {len(emitted)}/{len(files)}")
    if cache is not None and cache.added:
        log.info(f"  Cached {cache.added} new line(s) for future runs")

    return completed, warnings, still_missing
