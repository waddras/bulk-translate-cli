#!/usr/bin/env python3
"""Translate flow — extract (if vid), translate, and output."""
from pathlib import Path

from btcli.discover import discover_files


def run_translate(path: str, source_lang: str, target_lang: str, input_type: str,
                  file_filter: str, tracks: list, suffix: str, output_format: str, cfg: dict):
    """Run translation on discovered files."""
    # Always recursive for translate (unless path is a file)
    files = discover_files(path, input_type, file_filter, "recursive", cfg)
    if not files:
        print("[translate] No files found matching criteria.")
        return

    print(f"[translate] Found {len(files)} files")
    print(f"[translate] Language: {source_lang} -> {target_lang}")
    print(f"[translate] Tracks: {tracks}")
    print(f"[translate] Suffix: {suffix}")
    print(f"[translate] Output: {output_format or 'match source'}")
    print("=" * 60)

    # TODO: Implement full translation pipeline
    # 1. If input_type == "vid": extract subtitle tracks from each file
    # 2. If multiple tracks: combine them
    # 3. Parse subtitle files (srt_pre)
    # 4. Build blob, dedup, split
    # 5. Send to Gemini
    # 6. Reassemble output files

    for f in files:
        print(f"  {f.name}")

    print("\n" + "=" * 60)
    print(f"[translate] Pipeline not yet implemented. {len(files)} files queued.")
    print("[translate] Use the web UI (bulk-translate) for now.")
