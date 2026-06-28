"""Probe flow: inspect video/subtitle files for tracks, styles, and tags.

Supports:
  - -i vid: probe video files for subtitle tracks (via ffprobe)
  - -i sub: probe subtitle files for styles and tags
  - -m sample/recursive: discovery mode
  - -o tracks,styles,tags: what info to output
"""
from __future__ import annotations

from pathlib import Path

from .config import cfg
from .discover import discover_files
from .extract import probe_tracks
from .srt_pre import get_styles_from_file, scan_tags_from_file

SEP = "=" * 60


# ── Probe Video Files ─────────────────────────────────────────────────────────

def _probe_video_files(files: list, show_tags: bool = False) -> dict:
    """Probe video files for subtitle tracks.

    Args:
        files: list of Path objects to video files
        show_tags: if True, extract a track and scan for ASS tags

    Returns:
        {filepath: [tracks]} dict
    """
    results = {}

    for i, fpath in enumerate(files, 1):
        fpath = Path(fpath)
        if not fpath.exists():
            print(f"  [{i:02d}] {fpath.name} - NOT FOUND")
            continue

        try:
            tracks = probe_tracks(str(fpath))
            results[str(fpath)] = tracks
            print(f"  [{i:02d}] {fpath.name}:")

            if not tracks:
                print(f"        No subtitle tracks found")
                continue

            for t in tracks:
                title = f' "{t["title"]}"' if t["title"] else ""
                print(f"        Track {t['index']}: [{t['language']}] ({t['codec']}){title}")

            # If tags requested, extract first track and scan
            if show_tags and tracks:
                try:
                    from .extract import extract_track
                    import tempfile
                    with tempfile.TemporaryDirectory() as tmpdir:
                        tmp_out = str(Path(tmpdir) / "probe_tags.ass")
                        extracted = extract_track(str(fpath), 0, tmp_out)
                        tags = scan_tags_from_file(extracted)
                        if tags:
                            print(f"        Tags (track 0): {', '.join(sorted(tags))}")
                except Exception as e:
                    print(f"        Tags: could not scan ({e})")

        except Exception as e:
            print(f"  [{i:02d}] {fpath.name} - ERROR: {e}")
            results[str(fpath)] = []

    return results


# ── Probe Subtitle Files ──────────────────────────────────────────────────────

def _probe_subtitle_files(files: list, show_tags: bool = False) -> dict:
    """Probe subtitle files for styles and optionally tags.

    Args:
        files: list of Path objects to subtitle files
        show_tags: if True, also scan for unique ASS override tags

    Returns:
        {filepath: {styles: [...], tags: [...]}} dict
    """
    results = {}

    for i, fpath in enumerate(files, 1):
        fpath = Path(fpath)
        if not fpath.exists():
            print(f"  [{i:02d}] {fpath.name} - NOT FOUND")
            continue

        info = {"styles": [], "tags": []}

        # Styles
        styles = get_styles_from_file(fpath)
        info["styles"] = styles

        # Tags
        if show_tags:
            tags = scan_tags_from_file(fpath)
            info["tags"] = sorted(tags)

        results[str(fpath)] = info

        # Print
        size_kb = fpath.stat().st_size / 1024
        print(f"  [{i:02d}] {fpath.name}  ({size_kb:.1f} KB)")
        if styles:
            print(f"        Styles: {', '.join(styles)}")
        else:
            print(f"        Styles: (none / SRT format)")
        if show_tags and info["tags"]:
            print(f"        Tags: {', '.join(info['tags'])}")

    return results


# ── Main Probe Runner ─────────────────────────────────────────────────────────

def run_probe(path: str, input_type: str = "vid", scan_mode: str = "sample",
              filter_pattern: str | None = None, outputs: str = "tracks,styles") -> dict:
    """Run the probe flow.

    Args:
        path: file or directory to probe
        input_type: "vid" or "sub"
        scan_mode: "sample" or "recursive"
        filter_pattern: filename filter substring
        outputs: comma-separated output types (tracks, styles, tags)

    Returns:
        Results dict (structure depends on input_type)
    """
    output_parts = [o.strip() for o in outputs.split(",")]
    show_tags = "tags" in output_parts

    # Discover files
    mode = "vid" if input_type == "vid" else "sub"
    files = discover_files(path, mode=mode, scan_mode=scan_mode, filter_pattern=filter_pattern)

    if not files:
        kind = "video" if input_type == "vid" else "subtitle"
        print(f"No {kind} files found in: {path}")
        if filter_pattern:
            print(f"  (filter: '{filter_pattern}')")
        return {}

    print(SEP)
    print(f"PROBE - {len(files)} file(s) [{input_type}] [{scan_mode}]")
    if filter_pattern:
        print(f"  Filter: '{filter_pattern}'")
    print(SEP)

    # Run probe
    if input_type == "vid":
        results = _probe_video_files(files, show_tags=show_tags)
    else:
        results = _probe_subtitle_files(files, show_tags=show_tags)

    print(SEP)
    print(f"PROBE COMPLETE - {len(results)} files analyzed")

    # Summary for subtitle probe
    if input_type == "sub":
        all_styles = set()
        all_tags = set()
        for info in results.values():
            if isinstance(info, dict):
                all_styles.update(info.get("styles", []))
                all_tags.update(info.get("tags", []))
        if all_styles:
            print(f"  All styles: {', '.join(sorted(all_styles))}")
        if show_tags and all_tags:
            print(f"  All tags: {', '.join(sorted(all_tags))}")

    return results
