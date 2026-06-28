"""File discovery: find subtitle or MKV files with sample/recursive modes.

Discovery logic:
  - If path is a file → just that file
  - If path is a dir:
    - Looks one level into subdirs (not deeper into sub-subdirs)
    - sample mode: picks one file from each subdir
    - recursive mode: all matching files in all subdirs
  - Skips directories in SKIP_DIRS config list
  - Filter: match substring pattern in filename (e.g. ".en.ass")
"""
import random
from pathlib import Path

from .config import cfg


def _get_extensions(mode: str) -> set:
    """Return valid extensions for the given mode."""
    if mode == "vid":
        return set(cfg.get("MKV_EXTENSIONS", [".mkv", ".mp4", ".avi"]))
    return set(cfg.get("SOURCE_EXTENSIONS", [".srt", ".ass", ".ssa"]))


def _is_skipped(dirpath: Path) -> bool:
    """Check if a directory name is in the skip list."""
    skip_dirs = cfg.get("SKIP_DIRS", [])
    return dirpath.name in skip_dirs


def _matches_filter(filepath: Path, filter_pattern: str | None) -> bool:
    """Check if filename matches the filter pattern (substring match)."""
    if not filter_pattern:
        return True
    return filter_pattern.lower() in filepath.name.lower()


def discover_files(
    path: str,
    mode: str = "sub",
    scan_mode: str = "recursive",
    filter_pattern: str | None = None,
) -> list:
    """Discover files at the given path.

    Args:
        path: file or directory path
        mode: "sub" for subtitle files, "vid" for video/MKV files
        scan_mode: "sample" (one per subdir) or "recursive" (all matching)
        filter_pattern: substring to match in filename (e.g. ".en.ass")

    Returns:
        Sorted list of Path objects
    """
    target = Path(path).resolve()
    extensions = _get_extensions(mode)

    # Single file
    if target.is_file():
        if target.suffix.lower() in extensions and _matches_filter(target, filter_pattern):
            return [target]
        return []

    if not target.is_dir():
        return []

    if scan_mode == "sample":
        return _discover_sample(target, extensions, filter_pattern)
    else:
        return _discover_recursive(target, extensions, filter_pattern)


def _discover_recursive(root: Path, extensions: set, filter_pattern: str | None) -> list:
    """Find all matching files in root and one level of subdirs."""
    files = []

    # Files in root directory
    for f in root.iterdir():
        if f.is_file() and f.suffix.lower() in extensions and _matches_filter(f, filter_pattern):
            files.append(f)

    # Files in immediate subdirectories
    for subdir in sorted(root.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith(".") or _is_skipped(subdir):
            continue
        for f in subdir.rglob("*"):
            if f.is_file() and f.suffix.lower() in extensions and _matches_filter(f, filter_pattern):
                files.append(f)

    return sorted(files)


def _discover_sample(root: Path, extensions: set, filter_pattern: str | None) -> list:
    """Pick one matching file from each immediate subdir (for probing)."""
    files = []

    # Check root itself
    root_files = [
        f for f in root.iterdir()
        if f.is_file() and f.suffix.lower() in extensions and _matches_filter(f, filter_pattern)
    ]
    if root_files:
        files.append(sorted(root_files)[0])

    # One from each subdir
    for subdir in sorted(root.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith(".") or _is_skipped(subdir):
            continue
        subdir_files = [
            f for f in subdir.iterdir()
            if f.is_file() and f.suffix.lower() in extensions and _matches_filter(f, filter_pattern)
        ]
        if subdir_files:
            files.append(sorted(subdir_files)[0])

    return sorted(files)


def list_files(
    path: str,
    mode: str = "sub",
    scan_mode: str = "recursive",
    filter_pattern: str | None = None,
) -> None:
    """Print discovered files."""
    files = discover_files(path, mode, scan_mode, filter_pattern)
    kind = "video" if mode == "vid" else "subtitle"
    if not files:
        print(f"No {kind} files found in: {path}")
        return
    print(f"Found {len(files)} {kind} file(s):")
    for i, f in enumerate(files, 1):
        size_kb = f.stat().st_size / 1024
        rel = f.relative_to(Path(path).resolve()) if f.is_relative_to(Path(path).resolve()) else f.name
        print(f"  [{i:02d}] {rel}  ({size_kb:.1f} KB)")
