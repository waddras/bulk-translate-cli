"""Probe flow: inspect MKV files for subtitle tracks and extract them."""
import json
import subprocess
from pathlib import Path

from .config import cfg


def probe_file(filepath: str) -> list:
    """Probe an MKV file and return its subtitle tracks.

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


def extract_subtitle(filepath: str, track_index: int, suffix: str,
                     codec: str = "ass", convert_to_srt: bool = False) -> str:
    """Extract a single subtitle track from an MKV using -c:s copy.

    Returns: path to the extracted subtitle file.
    """
    if codec in ("ass", "ssa"):
        ext = "ass"
    else:
        ext = "srt"

    fpath = Path(filepath)
    out_name = fpath.stem + suffix + "." + ext
    out_path = fpath.parent / out_name

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

    # Convert to SRT if requested and source was ASS
    if convert_to_srt and ext == "ass":
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


def run_probe(file_paths: list) -> dict:
    """Probe all MKV files and print track info.

    Returns {filepath: [tracks]} dict.
    """
    print(f"{'=' * 60}")
    print(f"PROBE - {len(file_paths)} file(s)")
    print(f"{'=' * 60}")

    results = {}
    for i, fp in enumerate(file_paths, 1):
        fpath = Path(fp)
        if not fpath.exists():
            print(f"  [{i:02d}] {fpath.name} - NOT FOUND")
            continue
        try:
            tracks = probe_file(str(fp))
            results[str(fp)] = tracks
            print(f"  [{i:02d}] {fpath.name}:")
            if not tracks:
                print(f"        No subtitle tracks found")
            for t in tracks:
                title = f' "{t["title"]}"' if t["title"] else ""
                print(f"        Track {t['index']}: [{t['language']}] ({t['codec']}){title}")
        except Exception as e:
            print(f"  [{i:02d}] {fpath.name} - ERROR: {e}")
            results[str(fp)] = []

    print(f"{'=' * 60}")
    print(f"PROBE COMPLETE - {len(results)} files analyzed")
    return results


def run_extract(file_paths: list, track_index: int, suffix: str,
                convert_to_srt: bool = False) -> None:
    """Extract subtitle track from all MKV files."""
    print(f"{'=' * 60}")
    print(f"EXTRACT - {len(file_paths)} files, track {track_index}, suffix: '{suffix}'")

    # Determine codec from first file
    codec = "ass"
    try:
        tracks = probe_file(file_paths[0])
        if track_index < len(tracks):
            codec = tracks[track_index].get("codec", "ass")
    except Exception:
        pass

    ext = "ass" if codec in ("ass", "ssa") else "srt"
    print(f"Source codec: {codec} -> output: .{ext}")
    if convert_to_srt and ext == "ass":
        print(f"Will convert to SRT after extraction")
    print(f"{'=' * 60}")

    completed, failed = [], []
    for i, fp in enumerate(file_paths, 1):
        fpath = Path(fp)
        print(f"  [{i:02d}/{len(file_paths)}] {fpath.name}")
        try:
            out = extract_subtitle(str(fp), track_index, suffix, codec, convert_to_srt)
            print(f"        -> {Path(out).name}")
            completed.append(Path(out).name)
        except Exception as e:
            print(f"        ERROR: {e}")
            failed.append(fpath.name)

    print(f"{'=' * 60}")
    print(f"EXTRACT COMPLETE - {len(completed)} extracted, {len(failed)} failed")
    for f in completed:
        print(f"  done: {f}")
    for f in failed:
        print(f"  FAILED: {f}")
