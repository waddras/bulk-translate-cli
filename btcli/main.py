#!/usr/bin/env python3
"""CLI entry point for bulk-translate-cli.

Two subcommands:
    btcli probe    — inspect files for subtitle tracks, styles, tags
    btcli translate — full translation pipeline

Verbosity flags (global, before subcommand):
    --quiet    minimal output (timestamps + summaries only)
    --verbose  full output (debug details, per-attempt logs)
    (default)  medium output (rich progress bars, colored, ETA)
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .logger import log


def _parse_args():
    parser = argparse.ArgumentParser(
        prog="btcli",
        description="Bulk subtitle translation CLI",
    )
    parser.add_argument("--version", action="version", version=f"btcli {__version__}")

    # Global verbosity flags
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", "-q", action="store_true",
                           help="Minimal output: timestamps + phase summaries only")
    verbosity.add_argument("--verbose", "-v", action="store_true",
                           help="Full output: debug details, per-attempt logs, structured levels")

    # Log file
    parser.add_argument("--log-file", default=None, metavar="PATH",
                        help="Write all output to a log file (regardless of verbosity level)")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── probe ─────────────────────────────────────────────────────────────────
    p_probe = sub.add_parser("probe", help="Probe files for subtitle tracks, styles, and tags")
    p_probe.add_argument("-p", required=True, help="Path (file or directory)")
    p_probe.add_argument("-i", default="vid", choices=["vid", "sub"],
                         help="Input type: vid (video files) or sub (subtitle files). Default: vid")
    p_probe.add_argument("-m", default="sample", choices=["sample", "recursive"],
                         help="Mode: sample (one per subdir) or recursive (all). Default: sample")
    p_probe.add_argument("-f", default=None, metavar="FILTER",
                         help="Filter by filename pattern (substring match)")
    p_probe.add_argument("-o", default="tracks,styles", metavar="OUTPUTS",
                         help="Output info: tracks,styles,tags (comma-separated). Default: tracks,styles")

    # ── translate ─────────────────────────────────────────────────────────────
    p_trans = sub.add_parser("translate", help="Translate subtitle files (full pipeline)")
    p_trans.add_argument("-p", required=True, help="Path (file or directory)")
    p_trans.add_argument("-l", default="arabic", metavar="LANG",
                         help="Target language, or 'source,target' pair. Default: arabic")
    p_trans.add_argument("-i", default="vid", choices=["vid", "sub"],
                         help="Input type: vid (extract from video) or sub (subtitle files). Default: vid")
    p_trans.add_argument("-f", default=None, metavar="FILTER",
                         help="Filter by filename pattern (substring match)")
    p_trans.add_argument("-t", default="0", metavar="TRACKS",
                         help="Track number(s) to extract, comma-separated (only with -i vid). Default: 0")
    p_trans.add_argument("-s", "--styles", default=None, metavar="STYLES",
                         help="Keep only these ASS styles (comma-separated names). Default: auto-pick top N by unique line count")
    p_trans.add_argument("--passthrough", default=None, metavar="STYLES",
                         help="Include these styles untranslated (comma-separated names). Kept as-is from source.")
    p_trans.add_argument("-suffix", default=None, metavar="SUFFIX",
                         help="Output filename suffix. Default: auto from target lang (e.g. .ar)")
    p_trans.add_argument("-o", default=None, choices=["srt"],
                         help="Force output format: srt (force SRT from ASS source)")
    p_trans.add_argument("--show-name", default="", metavar="NAME",
                         help="Override auto-detected show name for translation prompt")

    # ── fix ───────────────────────────────────────────────────────────────────
    p_fix = sub.add_parser("fix", help="Re-process translated files without API calls")
    p_fix.add_argument("-p", required=True, help="Path (file or directory)")
    p_fix.add_argument("-f", default=".ar.", metavar="FILTER",
                       help="Filter by filename pattern. Default: .ar.")
    p_fix.add_argument("--apply", default="all", metavar="FIXES",
                       help="Fixes to apply (comma-separated): rtl, font, style, linebreak, all. Default: all")
    p_fix.add_argument("--backup", action="store_true",
                       help="Save .bak backup before overwriting")

    # ── update ────────────────────────────────────────────────────────────────
    p_update = sub.add_parser("update", help="Pull latest code + merge new settings")

    return parser.parse_args()


def main():
    args = _parse_args()

    # Configure logger
    if args.quiet:
        log.set_level("minimal")
    elif args.verbose:
        log.set_level("full")
    else:
        log.set_level("medium")

    if args.log_file:
        log.set_log_file(args.log_file)

    log.start_timer()

    if not args.command:
        print("No command specified. Use --help for usage.")
        print("\nCommands:")
        print("  btcli probe -p <path> [-i vid|sub] [-m sample|recursive] [-f filter] [-o outputs]")
        print("  btcli translate -p <path> [-l lang] [-i vid|sub] [-f filter] [-t tracks] [-suffix .ar] [-o srt]")
        print("  btcli fix -p <path> [-f filter] [--apply rtl,font,style,linebreak,all]")
        print("  btcli update [--merge]")
        print("\nVerbosity:")
        print("  --quiet / -q     Minimal output (timestamps + summaries)")
        print("  --verbose / -v   Full output (debug details)")
        print("  (default)        Medium output (progress bars, colors, ETA)")
        sys.exit(1)

    if args.command == "probe":
        from .probe import run_probe
        run_probe(
            path=args.p,
            input_type=args.i,
            scan_mode=args.m,
            filter_pattern=args.f,
            outputs=args.o,
        )

    elif args.command == "translate":
        from .translate import run_translate

        # Parse track indices
        track_indices = [int(t.strip()) for t in args.t.split(",") if t.strip()]

        # Parse styles
        styles = None
        if args.styles:
            styles = [s.strip() for s in args.styles.split(",") if s.strip()]

        # Parse passthrough styles
        passthrough = None
        if args.passthrough:
            passthrough = [s.strip() for s in args.passthrough.split(",") if s.strip()]

        run_translate(
            path=args.p,
            lang=args.l,
            input_type=args.i,
            filter_pattern=args.f,
            track_indices=track_indices,
            suffix=args.suffix,
            force_srt=(args.o == "srt"),
            show_name=args.show_name,
            keep_styles=styles,
            passthrough_styles=passthrough,
        )

    elif args.command == "fix":
        from .fix import run_fix
        run_fix(
            path=args.p,
            filter_pattern=args.f,
            apply=args.apply,
            backup=args.backup,
        )

    elif args.command == "update":
        from .update import run_update
        run_update()

    log.close()


if __name__ == "__main__":
    main()
