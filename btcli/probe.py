#!/usr/bin/env python3
"""Probe flow — inspect files for tracks, styles, and tags."""
import json
import re
import subprocess
from pathlib import Path

from btcli.discover import discover_files


def run_probe(path: str, mode: str, input_type: str, file_filter: str, output: str, cfg: dict):
    """Run probe on discovered files."""
    files = discover_files(path, input_type, file_filter, mode, cfg)
    if not files:
        print("[probe] No files found matching criteria.")
        return

    print(f"[probe] Found {len(files)} files (mode: {mode})")
    print("=" * 60)

    # Determine what to output
    default_output = cfg["defaults"].get("probe_output", "tracks,styles")
    if output == "tags":
        show_items = {"tracks", "styles", "tags"}
    elif output:
        show_items = set(output.split(","))
    else:
        show_items = set(default_output.split(","))

    all_tracks = []
    all_styles = set()
    all_tags = set()

    for f in files:
        print(f"\n  {f.name}")

        if "tracks" in show_items and input_type == "vid":
            tracks = _probe_tracks(f)
            all_tracks.extend(tracks)
            for t in tracks:
                title = f' "{t["title"]}"' if t["title"] else ""
                codec = _simplify_codec(t["codec"])
                print(f"    Track {t['index']}: [{t['language']}] ({codec}){title}")

        if ("styles" in show_items or "tags" in show_items) and f.suffix.lower() in (".ass", ".ssa"):
            content = f.read_text(encoding="utf-8", errors="replace")
            if "styles" in show_items:
                styles = _extract_styles(content)
                all_styles.update(styles)
            if "tags" in show_items:
                tags = _extract_tags(content)
                all_tags.update(tags)

    # Summary
    print("\n" + "=" * 60)
    print("[probe] Summary:")
    if "tracks" in show_items and all_tracks:
        unique_tracks = {}
        for t in all_tracks:
            key = f"{t['index']}-{t['language']}-{t['codec']}"
            if key not in unique_tracks:
                unique_tracks[key] = t
        print(f"  Unique tracks: {len(unique_tracks)}")
        for t in unique_tracks.values():
            codec = _simplify_codec(t["codec"])
            title = f' "{t["title"]}"' if t["title"] else ""
            print(f"    Track {t['index']}: [{t['language']}] ({codec}){title}")
    if "styles" in show_items and all_styles:
        print(f"  Unique styles: {', '.join(sorted(all_styles))}")
    if "tags" in show_items and all_tags:
        print(f"  Unique tags: {', '.join(sorted(all_tags))}")


def _probe_tracks(filepath: Path) -> list:
    """Probe MKV for subtitle tracks."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-select_streams", "s",
        str(filepath),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    tracks = []
    for i, stream in enumerate(data.get("streams", [])):
        tracks.append({
            "index": i,
            "codec": stream.get("codec_name", "unknown"),
            "language": stream.get("tags", {}).get("language", "und"),
            "title": stream.get("tags", {}).get("title", ""),
        })
    return tracks


def _extract_styles(content: str) -> list:
    """Extract style names from ASS content."""
    styles = []
    in_styles = False
    for line in content.splitlines():
        if line.strip().lower().startswith("[v4"):
            in_styles = True
            continue
        if in_styles and line.startswith("["):
            break
        if in_styles and line.startswith("Style:"):
            name = line.split(":", 1)[1].split(",")[0].strip()
            if name and name not in styles:
                styles.append(name)
    return styles


def _extract_tags(content: str) -> set:
    """Extract unique ASS override tag names from dialogue lines."""
    tags = set()
    tag_re = re.compile(r"\\([a-z]+)")
    for line in content.splitlines():
        if line.startswith("Dialogue:"):
            for match in re.finditer(r"\{([^}]*)\}", line):
                block = match.group(1)
                for tag_match in tag_re.finditer(block):
                    tags.add(tag_match.group(1))
    return tags


def _simplify_codec(codec: str) -> str:
    if codec in ("subrip", "mov_text"):
        return "srt"
    if codec in ("ass", "ssa"):
        return "ass"
    if codec == "webvtt":
        return "vtt"
    if codec in ("dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle"):
        return "image"
    return codec
