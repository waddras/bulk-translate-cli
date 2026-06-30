"""FFmpeg extraction: extract subtitle tracks from video files + multi-track combining.

Responsibilities:
  - Probe video files for subtitle track info (ffprobe)
  - Extract single or multiple tracks via ffmpeg
  - Combine multiple tracks into one subtitle file (concatenate events by timestamp)
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pysubs2

from .config import cfg
from .logger import log


# ── Probe ─────────────────────────────────────────────────────────────────────

def probe_tracks(filepath: str) -> list:
    """Probe a video file and return its subtitle tracks.

    Returns list of dicts: [{index, stream_index, codec, language, title}, ...]
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "s",
        str(filepath),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    tracks = []
    for i, stream in enumerate(data.get("streams", [])):
        tracks.append({
            "index": i,
            "stream_index": stream.get("index"),
            "codec": stream.get("codec_name", "unknown"),
            "language": stream.get("tags", {}).get("language", "und"),
            "title": stream.get("tags", {}).get("title", ""),
        })
    return tracks


# ── Single Track Extraction ───────────────────────────────────────────────────

def extract_track(filepath: str, track_index: int, output_path: str | None = None,
                  force_srt: bool = False) -> str:
    """Extract a single subtitle track from a video file.

    Args:
        filepath: path to video file
        track_index: subtitle stream index (0-based within subtitle streams)
        output_path: explicit output path (if None, auto-generated next to source)
        force_srt: if True, convert ASS output to SRT

    Returns:
        Path to the extracted subtitle file.
    """
    fpath = Path(filepath)

    # Determine codec
    tracks = probe_tracks(filepath)
    if track_index >= len(tracks):
        raise IndexError(f"Track {track_index} not found (file has {len(tracks)} subtitle tracks)")

    codec = tracks[track_index].get("codec", "ass")
    ext = "srt" if codec in ("subrip", "srt") else "ass"

    if output_path:
        out_path = Path(output_path)
        # Correct extension if it doesn't match the codec
        if ext == "srt" and out_path.suffix.lower() == ".ass":
            out_path = out_path.with_suffix(".srt")
        elif ext == "ass" and out_path.suffix.lower() == ".srt":
            out_path = out_path.with_suffix(".ass")
    else:
        out_path = fpath.with_suffix(f".track{track_index}.{ext}")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(fpath),
        "-map", f"0:s:{track_index}",
        "-c:s", "copy",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        err = result.stderr.strip()
        raise RuntimeError(f"ffmpeg extraction failed: {err[-300:]}")

    # Convert to SRT if requested
    if force_srt and ext == "ass":
        srt_path = out_path.with_suffix(".srt")
        convert_cmd = [
            "ffmpeg", "-y",
            "-i", str(out_path),
            "-c:s", "srt",
            str(srt_path),
        ]
        conv_result = subprocess.run(convert_cmd, capture_output=True, text=True, timeout=60)
        if conv_result.returncode == 0:
            out_path.unlink()
            return str(srt_path)

    return str(out_path)


# ── Multi-Track Combining ─────────────────────────────────────────────────────

def extract_and_combine_tracks(filepath: str, track_indices: list,
                               output_path: str | None = None,
                               force_srt: bool = False) -> str:
    """Extract multiple subtitle tracks and combine them into one file.

    Events from all tracks are merged and sorted by start time.
    Duplicate events (same start, end, text) are removed.

    Args:
        filepath: path to video file
        track_indices: list of track indices to extract and combine
        output_path: explicit output path
        force_srt: if True, output as SRT

    Returns:
        Path to the combined subtitle file.
    """
    if len(track_indices) == 1:
        return extract_track(filepath, track_indices[0], output_path, force_srt)

    fpath = Path(filepath)
    all_events = []
    output_ext = "srt" if force_srt else "ass"

    # Extract each track to a temp file, load, collect events
    with tempfile.TemporaryDirectory() as tmpdir:
        for idx in track_indices:
            tmp_out = str(Path(tmpdir) / f"track_{idx}.ass")
            try:
                extracted = extract_track(filepath, idx, tmp_out, force_srt=False)
                subs = pysubs2.SSAFile.load(extracted)
                for event in subs:
                    all_events.append({
                        "start": event.start,
                        "end": event.end,
                        "text": event.text,
                        "style": getattr(event, "style", "Default"),
                    })
            except Exception as e:
                log.detail(f"    Warning: failed to extract track {idx}: {e}")
                continue

    if not all_events:
        raise RuntimeError(f"No events extracted from tracks {track_indices}")

    # Sort by start time
    all_events.sort(key=lambda e: (e["start"], e["end"]))

    # Deduplicate (same start + end + text)
    seen = set()
    unique_events = []
    for ev in all_events:
        key = (ev["start"], ev["end"], ev["text"])
        if key not in seen:
            seen.add(key)
            unique_events.append(ev)

    # Build output file
    if output_path:
        out_path = Path(output_path)
    else:
        out_path = fpath.with_suffix(f".combined.{output_ext}")

    subs_out = pysubs2.SSAFile()
    for ev in unique_events:
        event = pysubs2.SSAEvent()
        event.start = ev["start"]
        event.end = ev["end"]
        event.text = ev["text"]
        event.style = ev.get("style", "Default")
        subs_out.append(event)

    subs_out.save(str(out_path))
    log.info(f"    Combined {len(track_indices)} tracks → {len(unique_events)} events "
             f"(from {len(all_events)} total)")
    return str(out_path)


# ── Track Selection ────────────────────────────────────────────────────────────

# Bitmap/image-based subtitle codecs that can't be extracted as text
_BITMAP_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


def find_first_text_track(filepath: str) -> int | None:
    """Find the first text-based subtitle track (skip PGS/bitmap).

    Returns track index or None if no text tracks found.
    """
    tracks = probe_tracks(filepath)
    for t in tracks:
        if t["codec"] not in _BITMAP_CODECS:
            return t["index"]
    return None


def validate_track_indices(filepath: str, track_indices: list) -> list:
    """Validate track indices — skip bitmap tracks, auto-select text if needed.

    If all requested tracks are bitmap, auto-selects first text track.
    Returns valid track indices list.
    """
    tracks = probe_tracks(filepath)

    if not tracks:
        return track_indices  # Let it fail downstream

    # Check if requested tracks are bitmap
    valid = []
    for idx in track_indices:
        if idx < len(tracks):
            if tracks[idx]["codec"] not in _BITMAP_CODECS:
                valid.append(idx)
            else:
                log.detail(f"    Skipping track {idx}: bitmap format ({tracks[idx]['codec']})")

    if valid:
        return valid

    # All requested tracks are bitmap — auto-select first text track
    log.info("    Requested track(s) are bitmap — auto-selecting first text track")
    first_text = find_first_text_track(filepath)
    if first_text is not None:
        log.info(f"    Auto-selected track {first_text} ({tracks[first_text]['codec']})")
        return [first_text]

    log.warning(f"    No text-based subtitle tracks found")
    return track_indices  # Let it fail downstream


# ── Batch Extraction (for translate flow) ─────────────────────────────────────

def extract_from_videos(video_files: list, track_indices: list,
                        suffix: str = "", force_srt: bool = False) -> list:
    """Extract subtitle tracks from multiple video files.

    Args:
        video_files: list of video file paths
        track_indices: track index(es) to extract (combined if multiple)
        suffix: output filename suffix (e.g. ".en")
        force_srt: convert to SRT

    Returns:
        List of paths to extracted subtitle files.
    """
    extracted = []

    for i, vpath in enumerate(video_files, 1):
        fpath = Path(vpath)
        log.item(f"[{i:02d}/{len(video_files)}] {fpath.name}")

        # Validate tracks — skip bitmap, auto-select text
        valid_tracks = validate_track_indices(str(fpath), track_indices)

        # Build output path — detect correct extension from codec
        try:
            tracks = probe_tracks(str(fpath))
            track_codec = tracks[valid_tracks[0]]["codec"] if valid_tracks[0] < len(tracks) else "ass"
        except Exception:
            track_codec = "ass"

        if force_srt or track_codec in ("subrip", "srt"):
            ext = "srt"
        else:
            ext = "ass"

        out_name = fpath.stem + suffix + "." + ext
        out_path = str(fpath.parent / out_name)

        try:
            if len(valid_tracks) > 1:
                result = extract_and_combine_tracks(str(fpath), valid_tracks, out_path, force_srt)
            else:
                result = extract_track(str(fpath), valid_tracks[0], out_path, force_srt)
            extracted.append(result)
            log.detail(f"        → {Path(result).name}")
        except Exception as e:
            log.detail(f"        ERROR: {e}")

    return extracted
