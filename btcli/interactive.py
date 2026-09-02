"""Interactive mode: guided per-directory track and style selection.

Flow:
  1. choose input type (video tracks or existing subtitle files)
  2. choose a path (defaults to the current directory)
  3. for every directory found, one level deep, sample a single file and choose
     the subtitle track, then the styles to translate
  4. choose whether to force re-extraction and how many files per API call
  5. review a summary, confirm, then translate each directory with its own
     settings

Style selection accepts the same syntax as -s, plus list numbers:
    +ALL,1          passthrough everything, translate only style 1
    1,2,+karaoke    translate styles 1 and 2, passthrough karaoke
    ALL,+karaoke    translate all styles, passthrough karaoke
    1,3,+ALL        translate styles 1 and 3, passthrough the rest
    +3              passthrough style 3
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .config import cfg, get_suffix_for_lang
from .discover import group_by_directory
from .logger import log
from .prompts import (
    Abort,
    ask as _ask,
    ask_choice as _ask_choice,
    ask_optional_int as _ask_optional_int,
    ask_yes_no as _ask_yes_no,
    bad as _bad,
    columns as _columns,
    good as _good,
    header as _header,
    hint as _hint,
    is_interactive,
    warn as _warn,
)

DEFAULT_STYLE_SELECTION = "ALL,+karaoke"


def _resolve_folder_selection(raw: str, count: int) -> list:
    """Turn a folder selection string into 1-based indices to include.

    Accepts ALL, a list of numbers to include, or a list of -numbers to
    exclude. Mixing include and exclude numbers is rejected as ambiguous.
    """
    pieces = [p.strip() for p in raw.split(",") if p.strip()]
    if not pieces:
        raise ValueError("nothing selected")

    if len(pieces) == 1 and pieces[0].lower() == "all":
        return list(range(1, count + 1))

    includes, excludes = [], []
    for piece in pieces:
        if piece.lower() == "all":
            raise ValueError("'ALL' cannot be combined with numbers")
        excluded = piece.startswith("-")
        body = piece[1:].strip() if excluded else piece
        if not body.isdigit():
            raise ValueError(f"'{piece}': use numbers, -numbers, or ALL")
        number = int(body)
        if not 1 <= number <= count:
            raise ValueError(f"'{piece}': pick a number between 1 and {count}")
        (excludes if excluded else includes).append(number)

    if includes and excludes:
        raise ValueError("cannot mix included and excluded numbers - use one or the other")

    chosen = ([i for i in range(1, count + 1) if i not in excludes]
              if excludes else sorted(set(includes)))
    if not chosen:
        raise ValueError("that would skip every folder")
    return chosen


def _ask_folders(entries: list) -> list:
    """Show the numbered folder list and return the entries to process."""
    if len(entries) == 1:
        return entries

    from .prompts import ITEM, paint
    print("\nFolders found:")
    for number, (directory, files) in enumerate(entries, 1):
        label = paint(f"  {number})", ITEM)
        print(f"{label} {directory.name or directory}   ({len(files)} file(s))")
    _hint("  Examples:")
    _hint("    ALL       every folder")
    _hint("    1,3       only folders 1 and 3")
    _hint("    -3        every folder except 3")

    while True:
        raw = _ask("  Folders to translate", "ALL")
        try:
            chosen = _resolve_folder_selection(raw, len(entries))
        except ValueError as exc:
            _bad(f"  {exc}")
            continue
        skipped = [entries[i - 1][0].name or entries[i - 1][0]
                   for i in range(1, len(entries) + 1) if i not in chosen]
        if skipped:
            _warn(f"  Skipping: {', '.join(str(s) for s in skipped)}")
        return [entries[i - 1] for i in chosen]


# ── Track selection ───────────────────────────────────────────────────────────

def _show_tracks(tracks: list, bitmap_codecs: set) -> None:
    print("  Tracks:")
    for track in tracks:
        codec = track.get("codec", "?")
        lang = track.get("language", "und")
        title = track.get("title", "")
        label = f'  [{track["index"]}] {codec:<20} {lang:<5}'
        if title:
            label += f' "{title}"'
        if codec in bitmap_codecs:
            label += "   (bitmap - cannot be translated)"
        print(label)


def _ask_track(tracks: list, bitmap_codecs: set) -> int | None:
    """Prompt for a text track index. Returns None when none are usable."""
    text_tracks = [t for t in tracks if t.get("codec") not in bitmap_codecs]
    if not text_tracks:
        return None

    valid = {t["index"] for t in text_tracks}
    default = str(text_tracks[0]["index"])
    while True:
        answer = _ask("  Track to translate", default)
        if not answer.isdigit():
            _bad("  Enter a track number from the list above")
            continue
        chosen = int(answer)
        if chosen in valid:
            return chosen
        if any(t["index"] == chosen for t in tracks):
            _bad("  That track is a bitmap image track and cannot be translated")
        else:
            _bad("  No such track number")


# ── Style selection ───────────────────────────────────────────────────────────

def _resolve_style_tokens(raw: str, styles: list) -> list:
    """Turn a selection string into concrete -s tokens.

    Numbers refer to the displayed list and may carry a + prefix. Raises
    ValueError with a readable message when a token cannot be resolved.
    """
    tokens = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue

        lowered = piece.lower()
        if lowered == "all":
            tokens.append("ALL")
            continue
        if lowered == "+all":
            tokens.append("+ALL")
            continue
        if lowered == "+karaoke":
            tokens.append("+karaoke")
            continue

        prefix = ""
        body = piece
        if piece.startswith("+"):
            prefix, body = "+", piece[1:].strip()

        if body.isdigit():
            number = int(body)
            if not 1 <= number <= len(styles):
                raise ValueError(f"'{piece}': pick a number between 1 and {len(styles)}")
            tokens.append(prefix + styles[number - 1])
        else:
            if body not in styles:
                raise ValueError(f"'{body}' is not a style in this track")
            tokens.append(prefix + body)

    if not tokens:
        raise ValueError("nothing selected")
    return tokens


def _ask_styles(styles: list, where: str) -> tuple:
    """Show the numbered style list and return (raw_input, keep, passthrough)."""
    from .styles import parse_styles_arg

    print(f"  Styles in {where}:")
    _columns([f"{i}) {name}" for i, name in enumerate(styles, 1)])
    _hint("  Examples:")
    _hint("    +ALL,1          passthrough all styles, translate only 1")
    _hint("    1,2,+karaoke    translate 1 and 2, passthrough karaoke")
    _hint("    ALL,+karaoke    translate all styles, passthrough karaoke")
    _hint("    1,3,+ALL        translate 1 and 3, passthrough the rest")
    _hint("    +3              passthrough style 3")

    while True:
        raw = _ask("  Styles", DEFAULT_STYLE_SELECTION)
        try:
            tokens = _resolve_style_tokens(raw, styles)
        except ValueError as exc:
            _bad(f"  {exc}")
            continue
        keep, passthrough = parse_styles_arg(",".join(tokens))
        return ",".join(tokens), keep, passthrough


# ── Sampling ──────────────────────────────────────────────────────────────────

def _styles_from_video(video: Path, track_index: int) -> list:
    """Extract one track to a temporary file to read its style names."""
    from .extract import extract_track
    from .srt_pre import get_styles_from_file

    with tempfile.TemporaryDirectory() as tmp:
        target = str(Path(tmp) / "sample.ass")
        extracted = extract_track(str(video), track_index, target, force_srt=False)
        if Path(extracted).suffix.lower() not in (".ass", ".ssa"):
            return []
        return get_styles_from_file(extracted)


def _output_marker() -> str:
    """Filename marker identifying this target language's own output."""
    codes = cfg.get("LANGUAGE_CODES", {})
    target = cfg.get("TARGET_LANGUAGE", "arabic").lower()
    return f".{codes.get(target, target[:2])}."


def _is_prior_output(path: Path) -> bool:
    """True when a subtitle file looks like output from an earlier run."""
    return _output_marker() in path.name.lower()


def _subtitle_files_in(directory: Path) -> list:
    """Subtitle files directly inside a directory, excluding prior output."""
    extensions = set(cfg.get("SOURCE_EXTENSIONS", [".srt", ".ass", ".ssa"]))
    return [
        item for item in sorted(directory.iterdir())
        if item.is_file()
        and item.suffix.lower() in extensions
        and not _is_prior_output(item)
    ]


# ── Per-directory planning ────────────────────────────────────────────────────

def _plan_video_directory(directory: Path, videos: list) -> dict | None:
    """Ask for track and styles for one directory of videos."""
    from .extract import _BITMAP_CODECS, probe_tracks

    sample = videos[0]
    print(f"\n  Sampling: {sample.name}")

    try:
        tracks = probe_tracks(str(sample))
    except Exception as exc:
        _bad(f"  Could not probe this file: {exc}")
        tracks = []

    track_index = None
    if tracks:
        _show_tracks(tracks, _BITMAP_CODECS)
        track_index = _ask_track(tracks, _BITMAP_CODECS)

    if track_index is None:
        # Fall back to external subtitle files sitting next to the videos.
        subtitles = _subtitle_files_in(directory)
        if not subtitles:
            _warn("  No usable subtitle tracks and no subtitle files here - skipping")
            return None
        _warn(f"  No subtitle tracks found, but {len(subtitles)} subtitle file(s) are here.")
        if not _ask_yes_no("  Use those subtitle files instead?", True):
            _warn("  Skipping this folder")
            return None
        return _plan_subtitle_directory(directory, subtitles)

    styles = _styles_from_video(sample, track_index)
    if not styles:
        _hint("  This track has no ASS styles (plain text) - all lines will be translated")
        return {"dir": directory, "mode": "vid", "files": videos, "track": track_index,
                "styles_raw": "(no styles)", "keep": None, "passthrough": None}

    raw, keep, passthrough = _ask_styles(styles, f"track {track_index}")
    return {"dir": directory, "mode": "vid", "files": videos, "track": track_index,
            "styles_raw": raw, "keep": keep, "passthrough": passthrough}


def _plan_subtitle_directory(directory: Path, subtitles: list) -> dict | None:
    """Ask for styles for one directory of subtitle files."""
    from .srt_pre import get_styles_from_file

    sample = subtitles[0]
    print(f"  Sampling: {sample.name}")

    styles = get_styles_from_file(sample) if sample.suffix.lower() in (".ass", ".ssa") else []
    if not styles:
        _hint(f"  {len(subtitles)} file(s), no ASS styles - all lines will be translated")
        return {"dir": directory, "mode": "sub", "files": subtitles, "track": None,
                "styles_raw": "(no styles)", "keep": None, "passthrough": None}

    raw, keep, passthrough = _ask_styles(styles, sample.name)
    return {"dir": directory, "mode": "sub", "files": subtitles, "track": None,
            "styles_raw": raw, "keep": keep, "passthrough": passthrough}


# ── Summary ───────────────────────────────────────────────────────────────────

def _print_summary(plans: list, force: bool, files_per_call, suffix: str) -> None:
    from .prompts import HEADING, ITEM, paint
    _header(f"PLAN - {len(plans)} folder(s)")
    for plan in plans:
        track = "n/a" if plan["track"] is None else str(plan["track"])
        print("  " + paint(str(plan["dir"].name or plan["dir"]), ITEM))
        _hint(f"    files:  {len(plan['files'])} ({plan['mode']})")
        _hint(f"    track:  {track}")
        print(f"    styles: {plan['styles_raw']}")
    print()
    _hint(f"  Output suffix:      {suffix}")
    _hint(f"  Force re-extract:   {'yes' if force else 'no'}")
    _hint(f"  Files per API call: {files_per_call if files_per_call else 'auto'}")
    print(paint("=" * 60, HEADING))


# ── Entry point ───────────────────────────────────────────────────────────────

def run_interactive(path: str | None = None) -> None:
    """Guided translation: prompt per directory, then translate each one."""
    if not is_interactive():
        log.error("Interactive mode needs a terminal. Use 'btcli translate' for scripts.")
        return

    try:
        _run(path)
    except Abort as exc:
        print()
        log.warning(f"Cancelled ({exc}). Nothing was written.")
    except KeyboardInterrupt:
        print()
        log.warning("Cancelled. Nothing was written.")


def _run(path: str | None) -> None:
    target_lang = cfg.get("TARGET_LANGUAGE", "arabic")
    suffix = get_suffix_for_lang(target_lang)

    _header("INTERACTIVE")
    print(f"  Target language: {target_lang} (from settings.conf)")
    print(f"  Output suffix:   {suffix}")
    print("=" * 60)

    mode = _ask_choice("Input type: extract from video, or use subtitle files",
                       ["vid", "sub"], "vid")

    if path is None:
        path = _ask("Path", str(Path.cwd()))
    root = Path(path).expanduser()
    if not root.exists():
        log.error(f"Path does not exist: {root}")
        return

    grouped = group_by_directory(str(root), mode=mode)
    if not grouped:
        kind = "video" if mode == "vid" else "subtitle"
        log.error(f"No {kind} files found in: {root}")
        return

    print(f"\nFound {sum(len(v) for v in grouped.values())} file(s) "
          f"in {len(grouped)} folder(s), one level deep.")

    # Drop this run's own previous output before counting, so the folder list
    # shows how many files would actually be translated.
    entries = []
    for directory, files in grouped.items():
        if mode == "sub":
            files = [f for f in files if not _is_prior_output(Path(f))]
            if not files:
                print(f"  {directory.name or directory}: only previously "
                      f"translated files - skipping")
                continue
        entries.append((directory, [Path(f) for f in files]))

    if not entries:
        log.error("Nothing left to translate after skipping previous output.")
        return

    entries = _ask_folders(entries)

    plans = []
    for directory, files in entries:
        _header(f"FOLDER - {directory.name or directory}  ({len(files)} file(s))")
        if mode == "vid":
            plan = _plan_video_directory(directory, files)
        else:
            plan = _plan_subtitle_directory(directory, files)
        if plan:
            plans.append(plan)

    if not plans:
        log.warning("Nothing selected. Nothing was written.")
        return

    print()
    force = _ask_yes_no("Force re-extraction (ignore previous extractions)?", False)
    files_per_call = _ask_optional_int("Files per API call", "auto")

    _print_summary(plans, force, files_per_call, suffix)
    if not _ask_yes_no("Proceed with translation?", True):
        log.warning("Cancelled. Nothing was written.")
        return

    from .translate import run_translate

    results = []
    for index, plan in enumerate(plans, 1):
        _header(f"RUN {index}/{len(plans)} - {plan['dir'].name or plan['dir']}")
        try:
            outcome = run_translate(
                path=str(plan["dir"]),
                lang=target_lang,
                input_type=plan["mode"],
                track_indices=[plan["track"]] if plan["track"] is not None else None,
                keep_styles=plan["keep"],
                passthrough_styles=plan["passthrough"],
                force=force,
                files_per_call=files_per_call,
                preset_files=[str(f) for f in plan["files"]],
            )
            if outcome:
                results.append(outcome)
        except KeyboardInterrupt:
            print()
            log.warning("Interrupted. Translated lines are cached; re-run to continue.")
            return
        except Exception as exc:
            log.error(f"{plan['dir'].name or plan['dir']} failed: {exc}")
            continue

    # One retry offer covering every folder, after they have all been processed.
    from .retry import offer_retry, total_missing
    if total_missing(results):
        offer_retry(results, api_key="")
