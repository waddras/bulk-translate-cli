"""The translate flow: ties the whole pipeline together.

TWO LEVELS
    ``run_translate`` prepares a run and is deliberately a readable summary of
    the flow, with each step delegated to a helper above it. ``_translate_batch``
    does the work for one batch of files. Everything else in this file is a named
    step belonging to one of those two.

WHAT A RUN DOES
    1. resolve settings, open the job manifest, check the API key
    2. resolve input files — discover subtitles, or extract them from video
    3. resolve styles once for the whole run, so batches agree
    4. split the files into batches and translate each
    5. report, and hand back a result the caller can act on

WHAT A BATCH DOES
    1. build the deduplicated blob (``blob.build_blob``)
    2. subtract what the cache already knows
    3. split the remainder into chunks
    4. send them, writing each file the moment its lines are all present
    5. write or report whatever is still missing

BATCHES vs CHUNKS — easy to confuse
    A *batch* is a group of files (``FILES_PER_BATCH``) deduplicated together, so
    lines shared between episodes are translated once. A *chunk* is one API
    request within a batch (``MAX_LINES_PER_CHUNK``). Bigger batches dedupe
    better but build a bigger blob; bigger chunks mean fewer requests but lose
    more work when one fails.

WRITTEN AS IT GOES
    Nothing waits for the end. Translations are cached and files emitted as
    responses arrive, so an interrupted run keeps everything it had finished, and
    re-running it only sends what is genuinely missing.

RETURN VALUE
    A dict (see ``_build_result``) rather than a bool, because interactive mode
    reuses it to offer a retry or a passthrough pass without asking every
    question again.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from pathlib import Path
from typing import NamedTuple

from .ai import run_translation
from .batch import BatchWriter
from .blob import (
    build_blob,
    estimate_output_tokens,
    split_blob,
    split_blob_by_files,
)
from .config import cfg, get_suffix_for_lang
from .discover import discover_files, exclude_translated_output
from .extract import extract_from_videos
from .logger import log


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

# ── Resume decision ───────────────────────────────────────────────────────────

# Asked at most once per process: a series with many season folders should not
# ask the same question for every folder.
#
# Two guards keep this from interrupting an unattended job, because the question
# is only reached once a batch actually has cache hits, and that can happen for
# the first time long after the run began:
#
#   * The hits must predate the process (cache.from_earlier_run). A run grows the
#     shared cache as it goes, so season 1 fills it and season 2 would otherwise
#     mistake this run's own work for an earlier run's.
#   * Nothing may be asked once chunks have gone out (_work_started). Even with
#     the snapshot, a re-run whose first batch happens to be all-new lines would
#     reach the question at batch 2 — with the run already committed and nobody
#     watching. Reuse is assumed there, which is the prompt's own default.
_resume_choice: bool | None = None

# Flipped the moment the first chunk is dispatched. One-way: a run that has
# started sending must never stall on a keypress.
_work_started: bool = False


def reset_resume_choice() -> None:
    """Forget the resume answer and the work-started latch, so the next job asks."""
    global _resume_choice, _work_started
    _resume_choice = None
    _work_started = False


def mark_work_started() -> None:
    """Record that chunks have been dispatched, silencing the resume prompt."""
    global _work_started
    _work_started = True


def _ask_resume(earlier: int, missing: int, total: int, cache_path) -> bool:
    """Ask whether to reuse lines cached by an earlier run.

    Remembered for the rest of the process. *earlier* counts only lines that
    predate this run, *missing* is what would actually be sent if resuming, and
    *total* is every unique line in the batch — so the three numbers shown are
    the real ones and do not have to add up to each other.
    """
    global _resume_choice
    if _resume_choice is not None:
        return _resume_choice

    from .prompts import ask_yes_no, hint, is_interactive

    if not is_interactive() or not cfg.get("RESUME_PROMPT", True):
        _resume_choice = True
        return True

    if _work_started:
        # Deliberately not latched: this is not the user's answer, so a later
        # batch is still free to ask if it somehow gets the chance.
        log.detail("  Reusing lines cached by an earlier run without asking - "
                   "translation is already under way")
        return True

    percent = round(earlier / total * 100) if total else 0
    from .prompts import header
    header("CACHED TRANSLATIONS FOUND")
    print(f"  {earlier} of {total} line(s) ({percent}%) were already translated "
          f"in an earlier run.")
    hint(f"  Cache: {cache_path}")
    hint("  Resuming sends only the missing lines. Declining re-translates "
         "them and costs full quota.")
    _resume_choice = ask_yes_no(
        f"  Resume and translate only the {missing} missing line(s)?", True)
    if not _resume_choice:
        log.warning("  Ignoring lines cached by earlier runs - translating those "
                    "again. Work done by this run is still reused.")
    return _resume_choice


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


# ── Run setup ─────────────────────────────────────────────────────────────────

def _new_manifest_run(*, path: str, input_type: str, source_lang: str,
                      target_lang: str, suffix: str, track_indices: list | None,
                      force_srt: bool, force: bool, files_per_call: int | None,
                      dry_run: bool):
    """Open the job record for this run.

    A dry run records nothing, so it gets a null record rather than creating or
    modifying .btcli.json.
    """
    from .manifest import ManifestRun, NullManifestRun

    if dry_run:
        return NullManifestRun()
    return ManifestRun({
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


def _resolve_api_key(dry_run: bool) -> str | None:
    """The API key, or None when the run cannot proceed without one.

    A dry run never calls the API, so it does not need a key — which means a job
    can be previewed before one is set up.
    """
    api_key = cfg.get("GEMINI_API_KEY", "")
    if api_key:
        return api_key
    if dry_run:
        return ""
    from .setup_key import check_api_key
    return check_api_key() or None


def _log_run_header(path: str, input_type: str, suffix: str, force_srt: bool,
                    source_lang: str, target_lang: str, dry_run: bool) -> None:
    """Announce what this run is about to do, before anything is touched."""
    log.sep()
    log.phase(f"{'DRY RUN' if dry_run else 'TRANSLATE'} - {source_lang} → {target_lang}")
    log.stat("Path", path)
    log.stat("Input", f"{input_type} | Suffix: {suffix} | Force SRT: {force_srt}")
    if dry_run:
        log.info("  Nothing will be sent to the API, written, or recorded.")
    log.sep()


# ── Resolving what to translate ───────────────────────────────────────────────

class _Inputs(NamedTuple):
    """The subtitle files a run will work on.

    show_name is carried back because video extraction has to resolve it early,
    to record the series in the manifest. early_result is set when the run is
    finished without translating anything — a dry run over video input stops
    before extraction, since extraction writes files.
    """
    files: list
    show_name: str
    early_result: dict | None


def _resolve_input_files(*, path, input_type, filter_pattern, preset_files,
                         track_indices, auto_track, force, force_srt,
                         source_lang, suffix, show_name, manifest_run,
                         dry_run) -> _Inputs:
    """Find the subtitle files to translate, extracting them first if needed."""
    if input_type == "vid":
        return _extract_subtitle_files(
            path=path, filter_pattern=filter_pattern, preset_files=preset_files,
            track_indices=track_indices, auto_track=auto_track, force=force,
            force_srt=force_srt, source_lang=source_lang, show_name=show_name,
            manifest_run=manifest_run, dry_run=dry_run,
        )
    return _discover_subtitle_files(
        path=path, filter_pattern=filter_pattern, preset_files=preset_files,
        suffix=suffix, show_name=show_name,
    )


def _discover_subtitle_files(*, path, filter_pattern, preset_files, suffix,
                             show_name) -> _Inputs:
    """Find existing subtitle files, never treating our own output as a source."""
    files = [Path(f) for f in preset_files] if preset_files else discover_files(
        path, mode="sub", scan_mode="recursive", filter_pattern=filter_pattern)
    if not files:
        log.error(f"No subtitle files found in: {path}")
        if filter_pattern:
            log.detail(f"  (filter: '{filter_pattern}')")
        return _Inputs([], show_name, None)

    files, prior_output = exclude_translated_output(files, suffix)
    if prior_output:
        log.info(f"Skipping {len(prior_output)} previously translated "
                 f"file(s) ending in '{suffix}'")
        for item in prior_output[:5]:
            log.detail(f"  skipped: {Path(item).name}")
    if not files:
        log.error(f"Only previously translated files found in: {path}")
        return _Inputs([], show_name, None)

    return _Inputs(files, show_name, None)


def _resolve_tracks(video_files: list, track_indices: list | None,
                    auto_track: bool | None) -> list:
    """Which subtitle track(s) to pull out of each video."""
    if not auto_track:
        return track_indices or [0]

    from .auto import auto_select_track_from_files
    detected = auto_select_track_from_files(video_files)
    if detected is not None:
        return [detected]
    log.warning("Auto-detect failed, using track 0")
    return track_indices or [0]


def _extract_subtitle_files(*, path, filter_pattern, preset_files, track_indices,
                            auto_track, force, force_srt, source_lang,
                            show_name, manifest_run, dry_run) -> _Inputs:
    """Pull subtitle tracks out of video files, reusing earlier extractions."""
    video_files = [Path(f) for f in preset_files] if preset_files else discover_files(
        path, mode="vid", scan_mode="recursive", filter_pattern=filter_pattern)
    if not video_files:
        log.error(f"No video files found in: {path}")
        return _Inputs([], show_name, None)

    log.info(f"Found {len(video_files)} video file(s)")
    tracks = _resolve_tracks(video_files, track_indices, auto_track)

    if dry_run:
        return _Inputs([], show_name, _preview_extraction(video_files, tracks, path))

    log.info(f"Extracting track(s): {tracks}")
    log.sep()

    if not show_name:
        show_name = _detect_show_name(video_files)
    manifest_run.register_files(video_files, series=show_name)
    manifest_run.update_command(show_name=show_name, tracks=tracks)

    # Extract with the source language suffix so the file is kept. A previous
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
        return _Inputs([], show_name, None)

    return _Inputs([Path(f) for f in sub_files], show_name, None)


def _preview_extraction(video_files: list, tracks: list, path: str) -> dict:
    """Report what would be extracted, without extracting it.

    Extraction writes files, so a dry run stops here. Cue and line counts are
    only knowable once a track has been extracted, so say so plainly rather
    than guessing.
    """
    log.sep()
    log.phase("DRY RUN - extraction step")
    log.stat("Videos found", str(len(video_files)))
    log.stat("Track(s) that would be extracted", ", ".join(map(str, tracks)))
    for index, video in enumerate(video_files, 1):
        log.item(f"[{index:02d}] {Path(video).name}")
    log.info("  Extraction is skipped in a dry run, so cue and line counts "
             "are not available for video input.")
    log.info("  For full numbers, extract once and dry-run with -i sub, "
             "or run without --dry-run.")
    return {"completed": [], "warnings": [], "missing": {},
            "files": video_files, "path": path, "dry_run": True,
            "previews": []}


# ── Reporting ─────────────────────────────────────────────────────────────────

def _report_files(files: list) -> None:
    """List the resolved input files with their sizes.

    Worth printing even when it is long: it is the last chance to notice that
    discovery picked up the wrong thing before quota is spent.
    """
    log.sep()
    log.phase(f"FILES - {len(files)} subtitle file(s)")
    for index, item in enumerate(files, 1):
        item = Path(item)
        log.item(f"[{index:02d}] {item.name}  ({item.stat().st_size / 1024:.1f} KB)")


def _resolve_styles(files: list, keep_styles: list | None,
                    passthrough_styles: list | None, manifest_run) -> tuple:
    """Decide which styles are translated and which pass through untouched.

    Resolved once for the whole run, so every batch treats styles the same way.
    """
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
    return keep_styles, passthrough_styles


def _report_completion(completed: list, warnings: list, missing: dict) -> None:
    """Final summary for the whole run, after every batch has finished."""
    log.sep()
    log.summary("Translation Complete", [
        ("Files written", str(len(completed))),
        ("Warnings", str(len(warnings)) if warnings else "0"),
        ("Lines missing", str(len(missing)) if missing else "0"),
        ("Elapsed", log.elapsed()),
    ])
    for item in completed:
        log.success(f"  done: {item}")
    for warning in warnings:
        log.warning(warning)


def _build_result(*, completed, warnings, missing, files, path, source_lang,
                  target_lang, keep_styles, passthrough_styles, show_name,
                  suffix, force_srt) -> dict:
    """The dict the caller gets back.

    Interactive mode reuses these values to offer a retry or a passthrough pass
    without asking every question again.
    """
    return {
        "completed": completed,
        "warnings": warnings,
        "missing": missing,
        "files": files,
        "path": path,
        "lang": f"{source_lang},{target_lang}",
        "keep_styles": keep_styles,
        "passthrough_styles": passthrough_styles,
        "show_name": show_name,
        "suffix": suffix,
        "force_srt": force_srt,
    }


# ── Batching ──────────────────────────────────────────────────────────────────

def _split_into_batches(files: list) -> list:
    """Group files into batches that are deduplicated and chunked together.

    Sizes are evened out rather than leaving a tiny final batch, since a batch
    that shares more context translates more consistently.
    """
    batch_size = cfg.get("FILES_PER_BATCH", 25)
    if len(files) <= batch_size:
        return [files]

    count = (len(files) + batch_size - 1) // batch_size
    even = (len(files) + count - 1) // count
    batches = [files[i:i + even] for i in range(0, len(files), even)]
    log.info(f"Splitting into {len(batches)} batch(es) of ~{even} files")
    return batches


def _merge_missing(target: dict, batch_missing: dict) -> None:
    """Fold one batch's missing lines into the run total, keeping files unique."""
    for key, info in batch_missing.items():
        merged = target.setdefault(key, {"text": info["text"], "files": []})
        for name in info["files"]:
            if name not in merged["files"]:
                merged["files"].append(name)


def _run_batches(*, files, keep_styles, passthrough_styles, show_name,
                 source_lang, target_lang, api_key, suffix, force_srt,
                 files_per_call, manifest_run, use_cache, write_only,
                 allow_resume_prompt, dry_run, previews) -> tuple:
    """Translate every batch and merge the results.

    The manifest is closed whatever happens, so an interrupted run still leaves
    a readable record of what it managed to do.
    """
    batches = _split_into_batches(files)
    completed: list = []
    warnings: list = []
    missing: dict = {}

    try:
        for number, batch_files in enumerate(batches, 1):
            if len(batches) > 1:
                log.sep()
                log.phase(f"BATCH {number}/{len(batches)} — {len(batch_files)} file(s)")

            batch_completed, batch_warnings, batch_missing = _translate_batch(
                batch_files, keep_styles, passthrough_styles,
                show_name, source_lang, target_lang,
                api_key, suffix, force_srt,
                files_per_call=files_per_call,
                manifest_run=manifest_run,
                use_cache=use_cache,
                write_only=write_only,
                allow_resume_prompt=allow_resume_prompt,
                dry_run=dry_run,
                previews=previews,
            )
            completed.extend(batch_completed)
            warnings.extend(batch_warnings)
            _merge_missing(missing, batch_missing)
    finally:
        manifest_run.finish()

    return completed, warnings, missing


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
    write_only: bool = False,
    allow_resume_prompt: bool = True,
    dry_run: bool = False,
) -> dict | None:
    """Run the full translation pipeline.

    Reads in the order the pipeline runs: set up, resolve the input files,
    resolve the styles, translate each batch, report. Each step is a helper
    above so this stays a summary of the flow rather than the whole of it.

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
        use_cache: consult and update the per-series translation cache
        write_only: make no API calls; assemble files from cached lines and leave
            anything still missing in the source language
        dry_run: report the work and stop. Makes no request, writes no file, and
            records nothing in the manifest or cache.
    """
    source_lang, target_lang = _parse_language(lang)
    if suffix is None:
        suffix = get_suffix_for_lang(target_lang)
    if files_per_call is not None and files_per_call < 1:
        log.error("--files-per-call must be at least 1")
        return None

    manifest_run = _new_manifest_run(
        path=path, input_type=input_type, source_lang=source_lang,
        target_lang=target_lang, suffix=suffix, track_indices=track_indices,
        force_srt=force_srt, force=force, files_per_call=files_per_call,
        dry_run=dry_run,
    )

    api_key = _resolve_api_key(dry_run)
    if api_key is None:
        return None

    _log_run_header(path, input_type, suffix, force_srt,
                    source_lang, target_lang, dry_run)

    inputs = _resolve_input_files(
        path=path, input_type=input_type, filter_pattern=filter_pattern,
        preset_files=preset_files, track_indices=track_indices,
        auto_track=auto_track, force=force, force_srt=force_srt,
        source_lang=source_lang, suffix=suffix, show_name=show_name,
        manifest_run=manifest_run, dry_run=dry_run,
    )
    if inputs.early_result is not None:
        return inputs.early_result
    if not inputs.files:
        return None

    files, show_name = inputs.files, inputs.show_name
    if not show_name:
        show_name = _detect_show_name(files)
    manifest_run.register_files(files, series=show_name)
    manifest_run.update_command(show_name=show_name)

    _report_files(files)
    keep_styles, passthrough_styles = _resolve_styles(
        files, keep_styles, passthrough_styles, manifest_run)
    log.stat("Show name", show_name)

    previews: list = []
    completed, warnings, missing = _run_batches(
        files=files, keep_styles=keep_styles,
        passthrough_styles=passthrough_styles, show_name=show_name,
        source_lang=source_lang, target_lang=target_lang, api_key=api_key,
        suffix=suffix, force_srt=force_srt, files_per_call=files_per_call,
        manifest_run=manifest_run, use_cache=use_cache, write_only=write_only,
        allow_resume_prompt=allow_resume_prompt, dry_run=dry_run,
        previews=previews,
    )

    result = _build_result(
        completed=completed, warnings=warnings, missing=missing, files=files,
        path=path, source_lang=source_lang, target_lang=target_lang,
        keep_styles=keep_styles, passthrough_styles=passthrough_styles,
        show_name=show_name, suffix=suffix, force_srt=force_srt,
    )

    if dry_run:
        from .preview import render_total
        if len(previews) > 1:
            render_total(previews)
        result.update({"completed": [], "warnings": [], "missing": {},
                       "dry_run": True, "previews": previews})
        return result

    _report_completion(completed, warnings, missing)
    return result


# ── One batch ─────────────────────────────────────────────────────────────────

class _BatchAborted(Exception):
    """Raised when a batch cannot proceed. Carries any warning worth reporting."""

    def __init__(self, warnings=()):
        super().__init__("batch aborted")
        self.warnings = list(warnings)


def _early_failure_recorder(files: list, manifest_run, source_lang: str,
                            target_lang: str, suffix: str,
                            files_per_call: int | None):
    """Build a callback that records every file in the batch as failed.

    Used when a batch aborts before any file could be written, so the manifest
    explains why rather than simply omitting them.
    """
    def record(status: str, reason: str) -> None:
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
    return record


def _build_batch_blob(files: list, keep_styles: list | None, record_failure) -> tuple:
    """Build the deduplicated blob for a batch.

    Raises _BatchAborted when the batch holds no dialogue, or is larger than
    MAX_BLOB_LINES allows.
    """
    log.sep()
    log.phase("PHASE 1 - Building blob...")
    meta, payload, stats = build_blob(files, keep_styles=keep_styles)

    if stats["total"] == 0:
        log.warning("No dialogue cues found in this batch.")
        record_failure("no_dialogue", "No dialogue cues matched the selected styles")
        raise _BatchAborted()

    max_blob = cfg.get("MAX_BLOB_LINES", 50000)
    if stats["total"] > max_blob:
        log.error(f"Too many cues ({stats['total']} > {max_blob}). Reduce FILES_PER_BATCH.")
        reason = f"Batch exceeded MAX_BLOB_LINES ({stats['total']} > {max_blob})"
        record_failure("failed", reason)
        raise _BatchAborted([reason])

    log.info(f"DEDUP: {stats['total']} total → {stats['unique']} unique "
             f"({stats['collapsed']} collapsed, ~{stats['pct']}% fewer tokens)")
    return meta, payload, stats


def _report_cache_state(cache, cached_hits: dict, to_translate: dict, *,
                        earlier: int, ignored: int) -> None:
    """Always say what the cache did, so resuming is never a mystery.

    The headline counts every reused line, since that is what determines the
    quota spend. The earlier-run/this-run split goes underneath as detail: it
    matters for understanding the resume prompt but not for the decision.
    """
    if ignored:
        log.info(f"CACHE: {ignored} line(s) from an earlier run ignored by "
                 f"choice; {len(to_translate)} line(s) to translate")
        if cached_hits:
            log.detail(f"  Still reusing {len(cached_hits)} line(s) this run "
                       f"translated a moment ago")
    elif cached_hits:
        log.info(f"CACHE: {len(cached_hits)} line(s) already translated, "
                 f"{len(to_translate)} still to translate")
        if earlier != len(cached_hits):
            log.detail(f"  {earlier} from an earlier run, "
                       f"{len(cached_hits) - earlier} from this run")
        log.detail(f"  Cache file: {cache.path}")
    elif cache.loaded:
        log.info(f"CACHE: {cache.loaded} entry(ies) on file, none match this "
                 f"batch; {len(to_translate)} line(s) to translate")
    else:
        log.info(f"CACHE: empty so far; {len(to_translate)} line(s) to "
                 f"translate, cached as they complete")


def _resolve_cache(*, files, payload, target_lang, use_cache, write_only,
                   allow_resume_prompt) -> tuple:
    """Split the payload into lines already translated and lines still needed.

    Returns (cache, cached_hits, to_translate). cache is None when caching is
    off, in which case nothing is read from or written to disk.

    Declining the resume prompt moves the lines cached by earlier runs back into
    to_translate and leaves the rest alone, so "don't trust the old cache" does
    not also mean "re-buy what this run translated five minutes ago".
    """
    if not (use_cache and cfg.get("USE_TRANSLATION_CACHE", True)):
        if not use_cache:
            log.info("CACHE: disabled (--no-cache), translating every line")
        return None, {}, payload

    from .cache import TranslationCache, series_root_for
    cache = TranslationCache(series_root_for(files), target_lang)
    cached_hits, to_translate = cache.split(payload)

    # Only hits that predate this process are the resume prompt's business; see
    # the resume-decision comment above.
    earlier_tags = cache.from_earlier_run({tag: payload[tag] for tag in cached_hits})

    # Offer to resume. Asked once per run, and never during a retry or
    # passthrough pass, which are already an answer to this question.
    ignored = 0
    if earlier_tags and allow_resume_prompt and not write_only:
        if not _ask_resume(len(earlier_tags), len(to_translate), len(payload),
                           cache.path):
            # Declining distrusts what was on disk before the run started, not
            # the lines this run has just paid for. Re-sending those would spend
            # quota twice inside one job to no purpose.
            ignored = len(earlier_tags)
            for tag in earlier_tags:
                cached_hits.pop(tag, None)
                to_translate[tag] = payload[tag]

    _report_cache_state(cache, cached_hits, to_translate,
                        earlier=len(earlier_tags) - ignored, ignored=ignored)
    return cache, cached_hits, to_translate


def _plan_chunks(*, to_translate, meta, files_per_call, write_only) -> list:
    """Split what needs translating into API calls, and report the plan."""
    log.sep()
    log.phase("PHASE 2 - Splitting into chunks...")

    if write_only or not to_translate:
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
    for index, chunk in enumerate(chunks, 1):
        estimate = estimate_output_tokens(chunk)
        total_tokens += estimate
        log.detail(f"  Chunk {index}: {len(chunk)} lines, ~{estimate} output tokens")
    log.stat("Chunk sizes", ", ".join(str(len(chunk)) for chunk in chunks) or "none")
    log.stat("Total estimated output tokens", str(total_tokens))
    return chunks


def _preview_batch(*, files, stats, cached_hits, chunks, writer, suffix,
                   source_lang, target_lang, mode, files_per_call,
                   previews) -> None:
    """Report the shape of the work this batch would do, and record it."""
    from .preview import render, summarise

    summary = summarise(
        files=files, stats=stats, cached=len(cached_hits), chunks=chunks,
        required_by_file=writer.required_by_file,
        tolerance=max(0, cfg.get("PARTIAL_LINE_TOLERANCE", 10)),
        suffix=suffix, source_lang=source_lang, target_lang=target_lang,
        mode=mode, files_per_call=files_per_call,
    )
    render(summary)
    if previews is not None:
        previews.append(summary)


def _send_chunks(*, chunks, payload, api_key, show_name, source_lang,
                 target_lang, cache, to_translate, cached_hits, writer) -> dict:
    """Send every chunk, caching and writing results as they arrive.

    Each response is cached before any file is written, so an interrupted run
    never loses translated lines.
    """
    def on_progress(fresh: dict) -> None:
        if cache is not None:
            cache.store_and_flush(to_translate, fresh)
        writer.write_ready({**cached_hits, **fresh})

    log.sep()
    log.phase("PHASE 3 - Translating and writing completed files...")

    if not chunks:
        return {}

    # The run is now committed to spending quota. From here no prompt may block
    # it, however many batches or folders are still to come.
    mark_work_started()

    fresh: dict = {}
    log.start_progress("Translating", total=len(chunks))
    try:
        fresh = asyncio.run(
            run_translation(
                chunks, payload, api_key, show_name, source_lang, target_lang,
                progress_callback=on_progress,
            )
        )
    finally:
        # Always tear the live display down, even on error or Ctrl-C.
        log.finish_progress()
        if cache is not None:
            cache.store_and_flush(to_translate, fresh)
    return fresh


def _finalize_batch(*, writer, payload, translated_unique, write_only, cache,
                    files) -> dict:
    """Write or report whatever is left, then report how the batch went.

    Passthrough deliberately ignores the tolerance, since its whole purpose is
    to write every file regardless of how many lines are still in the source
    language.
    """
    log.sep()
    log.phase("PHASE 4 - Finalizing file completion...")
    tolerance = (len(payload) if write_only
                 else max(0, cfg.get("PARTIAL_LINE_TOLERANCE", 10)))
    still_missing = writer.finalize(translated_unique, tolerance)

    total_keys = len(payload)
    translated_count = len(set(payload) & set(translated_unique))
    pct = round(translated_count / total_keys * 100) if total_keys else 0
    log.info(f"  Batch: {translated_count}/{total_keys} unique lines ({pct}%)")
    log.info(f"  Files written: {len(writer.emitted)}/{len(files)}")
    if cache is not None and cache.added:
        log.info(f"  Cached {cache.added} new line(s) for future runs")
    return still_missing


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
    write_only: bool = False,
    allow_resume_prompt: bool = True,
    dry_run: bool = False,
    previews: list | None = None,
) -> tuple:
    """Translate one batch, writing each file as soon as its lines are ready.

    With write_only, nothing is sent to the API: files are assembled from cached
    lines alone and anything still missing is left in the source language. Used
    by the passthrough option after a job ends with lines missing.

    Returns (completed, warnings, still_missing), where still_missing maps a
    representative key to {"text": source line, "files": [output names]}.
    """
    record_failure = _early_failure_recorder(
        files, manifest_run, source_lang, target_lang, suffix, files_per_call)

    try:
        meta, payload, stats = _build_batch_blob(files, keep_styles, record_failure)
    except _BatchAborted as aborted:
        return [], aborted.warnings, {}

    cache, cached_hits, to_translate = _resolve_cache(
        files=files, payload=payload, target_lang=target_lang,
        use_cache=use_cache, write_only=write_only,
        allow_resume_prompt=allow_resume_prompt,
    )

    if write_only:
        log.info(f"PASSTHROUGH: writing from cache only, {len(to_translate)} "
                 f"line(s) will stay in {source_lang}. No API calls.")
    elif not to_translate:
        log.success("Every line was already translated - nothing to send to the API")

    chunks = _plan_chunks(to_translate=to_translate, meta=meta,
                          files_per_call=files_per_call, write_only=write_only)

    mode = cfg.get("TRANSLATION_MODE", "chunked")
    log.stat("Translation mode", mode)

    writer = BatchWriter(
        files, meta, payload, chunks,
        suffix=suffix, force_srt=force_srt,
        keep_styles=keep_styles, passthrough_styles=passthrough_styles,
        source_lang=source_lang, target_lang=target_lang,
        mode=mode, files_per_call=files_per_call, manifest_run=manifest_run,
    )

    # A dry run stops here: the shape of the work is known, so report it and
    # make no request, write no file, and record nothing.
    if dry_run:
        _preview_batch(
            files=files, stats=stats, cached_hits=cached_hits, chunks=chunks,
            writer=writer, suffix=suffix, source_lang=source_lang,
            target_lang=target_lang, mode=mode, files_per_call=files_per_call,
            previews=previews,
        )
        return [], [], {}

    fresh = _send_chunks(
        chunks=chunks, payload=payload, api_key=api_key, show_name=show_name,
        source_lang=source_lang, target_lang=target_lang, cache=cache,
        to_translate=to_translate, cached_hits=cached_hits, writer=writer,
    )

    translated_unique = {**cached_hits, **fresh}
    still_missing = _finalize_batch(
        writer=writer, payload=payload, translated_unique=translated_unique,
        write_only=write_only, cache=cache, files=files,
    )

    return writer.completed, writer.warnings, still_missing
