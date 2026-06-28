#!/usr/bin/env python3
"""CLI entry point for bulk-translate-cli.

Usage:
    python -m btcli probe /path/to/mkv/folder
    python -m btcli extract /path/to/folder --track 0 --suffix ".en"
    python -m btcli translate /path/to/subs/folder
    python -m btcli translate file1.srt file2.ass
    python -m btcli styles /path/to/ass/files
    python -m btcli list /path/to/folder
"""
import argparse
import sys

from . import __version__
from .config import cfg
from .discover import discover_files, list_files


def _parse_args():
    parser = argparse.ArgumentParser(
        prog="btcli",
        description="Bulk subtitle translation CLI (English → Arabic)",
    )
    parser.add_argument("--version", action="version", version=f"btcli {__version__}")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── probe ──
    p_probe = sub.add_parser("probe", help="Probe MKV files for subtitle tracks")
    p_probe.add_argument("path", help="File or directory to probe")
    p_probe.add_argument("-r", "--recursive", action="store_true")

    # ── extract ──
    p_extract = sub.add_parser("extract", help="Extract subtitle track from MKV files")
    p_extract.add_argument("path", help="File or directory")
    p_extract.add_argument("-t", "--track", type=int, default=0, help="Track index (default: 0)")
    p_extract.add_argument("-s", "--suffix", default=".en", help="Output suffix (default: .en)")
    p_extract.add_argument("--to-srt", action="store_true", help="Convert ASS to SRT after extract")
    p_extract.add_argument("-r", "--recursive", action="store_true")

    # ── translate ──
    p_trans = sub.add_parser("translate", help="Translate subtitle files (full pipeline)")
    p_trans.add_argument("path", nargs="+", help="File(s) or directory to translate")
    p_trans.add_argument("-r", "--recursive", action="store_true")
    p_trans.add_argument("--styles", nargs="*", help="Keep only these ASS styles")
    p_trans.add_argument("--show-name", default="", help="Override auto-detected show name")

    # ── styles ──
    p_styles = sub.add_parser("styles", help="Detect ASS styles in subtitle files")
    p_styles.add_argument("path", help="File or directory")
    p_styles.add_argument("-r", "--recursive", action="store_true")

    # ── list ──
    p_list = sub.add_parser("list", help="List discovered files")
    p_list.add_argument("path", help="Directory to scan")
    p_list.add_argument("-m", "--mode", choices=["translate", "extract"], default="translate")
    p_list.add_argument("-r", "--recursive", action="store_true")

    return parser.parse_args()



def main():
    args = _parse_args()

    if not args.command:
        print("No command specified. Use --help for usage.")
        sys.exit(1)

    if args.command == "list":
        list_files(args.path, args.mode, args.recursive)

    elif args.command == "probe":
        from .probe import run_probe
        files = discover_files(args.path, mode="extract", recursive=args.recursive)
        if not files:
            print(f"No MKV files found in: {args.path}")
            sys.exit(1)
        run_probe([str(f) for f in files])

    elif args.command == "extract":
        from .probe import run_extract
        files = discover_files(args.path, mode="extract", recursive=args.recursive)
        if not files:
            print(f"No MKV files found in: {args.path}")
            sys.exit(1)
        run_extract([str(f) for f in files], args.track, args.suffix, args.to_srt)

    elif args.command == "styles":
        from .translate import get_styles_from_files
        files = discover_files(args.path, mode="translate", recursive=args.recursive)
        if not files:
            print(f"No subtitle files found in: {args.path}")
            sys.exit(1)
        styles = get_styles_from_files([str(f) for f in files])
        if styles:
            print(f"Found {len(styles)} style(s):")
            for s in styles:
                print(f"  - {s}")
        else:
            print("No ASS styles found (files may be SRT format)")

    elif args.command == "translate":
        from .translate import run_translate
        # Collect all file paths from arguments
        all_files = []
        for p in args.path:
            found = discover_files(p, mode="translate", recursive=args.recursive)
            all_files.extend(found)
        if not all_files:
            print(f"No subtitle files found in: {args.path}")
            sys.exit(1)
        # Deduplicate while preserving order
        seen = set()
        unique_files = []
        for f in all_files:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)
        run_translate(
            [str(f) for f in unique_files],
            keep_styles=args.styles,
            show_name=args.show_name,
        )


if __name__ == "__main__":
    main()
