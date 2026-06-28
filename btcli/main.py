#!/usr/bin/env python3
"""CLI entry point for bulk-translate-cli.

Two subcommands:
    btcli probe    — inspect files for subtitle tracks, styles, tags
    btcli translate — full translation pipeline
"""
import argparse
import sys

from . import __version__


def _parse_args():
    parser = argparse.ArgumentParser(
        prog="btcli",
        description="Bulk subtitle translation CLI",
    )
    parser.add_argument("--version", action="version", version=f"btcli {__version__}")
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
    p_trans.add_argument("-i", default="sub", choices=["vid", "sub"],
                         help="Input type: vid (extract from video) or sub (subtitle files). Default: sub")
    p_trans.add_argument("-f", default=None, metavar="FILTER",
                         help="Filter by filename pattern (substring match)")
    p_trans.add_argument("-t", default="0", metavar="TRACKS",
                         help="Track number(s) to extract, comma-separated (only with -i vid). Default: 0")
    p_trans.add_argument("-suffix", default=None, metavar="SUFFIX",
                         help="Output filename suffix. Default: auto from target lang (e.g. .ar)")
    p_trans.add_argument("-o", default=None, choices=["srt"],
                         help="Force output format: srt (force SRT from ASS source)")
    p_trans.add_argument("--show-name", default="", metavar="NAME",
                         help="Override auto-detected show name for translation prompt")

    return parser.parse_args()


def main():
    args = _parse_args()

    if not args.command:
        print("No command specified. Use --help for usage.")
        print("\nCommands:")
        print("  btcli probe -p <path> [-i vid|sub] [-m sample|recursive] [-f filter] [-o outputs]")
        print("  btcli translate -p <path> [-l lang] [-i vid|sub] [-f filter] [-t tracks] [-suffix .ar] [-o srt]")
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

        run_translate(
            path=args.p,
            lang=args.l,
            input_type=args.i,
            filter_pattern=args.f,
            track_indices=track_indices,
            suffix=args.suffix,
            force_srt=(args.o == "srt"),
            show_name=args.show_name,
        )


if __name__ == "__main__":
    main()
