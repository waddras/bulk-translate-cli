#!/usr/bin/env python3
"""File discovery — finds files based on path, input type, filter, and mode."""
from pathlib import Path


def discover_files(path: str, input_type: str, file_filter: str, mode: str, cfg: dict) -> list:
    """Discover files matching criteria.

    Args:
        path: file or directory path
        input_type: "vid" or "sub"
        file_filter: filename pattern filter (e.g. ".en.ass") or None
        mode: "sample" (one per subdir) or "recursive" (all)
        cfg: full config dict

    Returns: list of Path objects
    """
    target = Path(path).resolve()

    if target.is_file():
        return [target]

    if not target.is_dir():
        print(f"[error] Path not found: {path}")
        return []

    # Determine extensions to look for
    if input_type == "vid":
        extensions = set(cfg.get("video_extensions", [".mkv", ".mp4", ".avi"]))
    else:
        extensions = set(cfg.get("subtitle_extensions", [".srt", ".ass", ".ssa"]))

    skip_dirs = set(cfg.get("skip_dirs", []))

    # Collect subdirs (one level deep)
    subdirs = []
    for item in sorted(target.iterdir()):
        if item.is_dir() and item.name not in skip_dirs and not item.name.startswith("."):
            subdirs.append(item)

    # If no subdirs, treat the target dir itself as the only "subdir"
    if not subdirs:
        subdirs = [target]

    results = []
    for subdir in subdirs:
        # Get all matching files in this subdir (recursive within subdir)
        matches = []
        for f in sorted(subdir.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in extensions:
                continue
            if file_filter and file_filter not in f.name:
                continue
            # Skip files in skip_dirs
            if any(part in skip_dirs for part in f.relative_to(subdir).parts[:-1]):
                continue
            matches.append(f)

        if mode == "sample" and matches:
            results.append(matches[0])  # Take first match from each subdir
        else:
            results.extend(matches)

    return results
