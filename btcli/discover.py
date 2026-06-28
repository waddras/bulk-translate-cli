"""File discovery: find subtitle or MKV files in a directory tree."""
from pathlib import Path

from .config import cfg


def discover_files(path: str, mode: str = "translate", recursive: bool = False) -> list:
    """Find subtitle or MKV files at the given path.

    Args:
        path: file or directory path
        mode: "translate" for subtitle files (.srt/.ass), "extract" for MKV files
        recursive: if True, search subdirectories

    Returns:
        Sorted list of Path objects
    """
    target = Path(path).resolve()

    if mode == "extract":
        extensions = set(cfg.get("MKV_EXTENSIONS", [".mkv", ".mp4", ".avi"]))
    else:
        extensions = set(cfg.get("SOURCE_EXTENSIONS", [".srt", ".ass"]))

    if target.is_file():
        if target.suffix.lower() in extensions:
            return [target]
        return []

    if not target.is_dir():
        return []

    if recursive:
        files = [f for f in target.rglob("*") if f.is_file() and f.suffix.lower() in extensions]
    else:
        files = [f for f in target.iterdir() if f.is_file() and f.suffix.lower() in extensions]

    return sorted(files)


def list_files(path: str, mode: str = "translate", recursive: bool = False) -> None:
    """Print discovered files (for --list flag)."""
    files = discover_files(path, mode, recursive)
    if not files:
        print(f"No {'MKV' if mode == 'extract' else 'subtitle'} files found in: {path}")
        return
    print(f"Found {len(files)} file(s):")
    for i, f in enumerate(files, 1):
        size_kb = f.stat().st_size / 1024
        print(f"  [{i:02d}] {f.name}  ({size_kb:.1f} KB)")
