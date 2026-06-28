#!/usr/bin/env python3
"""btcli — Bulk Translate CLI

Usage:
    btcli -probe -p "/path/to/show" [-m sample|recursive] [-i vid|sub] [-f ".en.ass"] [-o tags]
    btcli -translate -p "/path/to/show" [-l "arabic"] [-i vid|sub] [-f ".en.ass"] [-t 0] [-suffix ".ar"] [-o srt]
"""
import argparse
import sys
from pathlib import Path

from btcli.config import load_config
from btcli.discover import discover_files
from btcli.probe import run_probe
from btcli.translate import run_translate


def main():
    parser = argparse.ArgumentParser(
        prog="btcli",
        description="Bulk Subtitle Translator CLI",
    )
    # Main action (mutually exclusive)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("-probe", action="store_true", help="Probe files for tracks/styles/tags")
    action.add_argument("-translate", action="store_true", help="Translate subtitles")

    # Common flags
    parser.add_argument("-p", "--path", required=True, help="Target path (file or directory)")
    parser.add_argument("-i", "--input", default=None, help="Input type: vid or sub (default: vid)")
    parser.add_argument("-f", "--filter", default=None, help="Filter by filename pattern (e.g. .en.ass)")

    # Probe flags
    parser.add_argument("-m", "--mode", default=None, help="Probe mode: sample or recursive (default: sample)")
    parser.add_argument("-o", "--output", default=None, help="Output: tracks,styles,tags (probe) or srt (translate)")

    # Translate flags
    parser.add_argument("-l", "--lang", default=None, help="Language: 'target' or 'source,target' (default: english,arabic)")
    parser.add_argument("-t", "--tracks", default=None, help="Track numbers comma-separated (default: 0)")
    parser.add_argument("-suffix", default=None, help="Output suffix (default: auto from target lang)")

    args = parser.parse_args()
    cfg = load_config()

    # Resolve defaults
    input_type = args.input or cfg["defaults"]["input_type"]
    file_filter = args.filter

    if args.probe:
        mode = args.mode or cfg["defaults"]["probe_mode"]
        output = args.output  # None = default (tracks,styles), "tags" = add tags
        run_probe(
            path=args.path,
            mode=mode,
            input_type=input_type,
            file_filter=file_filter,
            output=output,
            cfg=cfg,
        )
    elif args.translate:
        # Parse language
        lang_str = args.lang or cfg["defaults"]["target_lang"]
        if "," in lang_str:
            source_lang, target_lang = [l.strip() for l in lang_str.split(",", 1)]
        else:
            source_lang = cfg["defaults"]["source_lang"]
            target_lang = lang_str.strip()

        # Parse tracks
        tracks_str = args.tracks or str(cfg["defaults"]["track"])
        tracks = [int(t.strip()) for t in tracks_str.split(",")]

        # Suffix
        if args.suffix:
            suffix = args.suffix
        else:
            lang_code = cfg["lang_codes"].get(target_lang.lower(), target_lang[:2])
            suffix = f".{lang_code}"

        # Output format
        output_format = args.output  # None = match source, "srt" = force SRT

        run_translate(
            path=args.path,
            source_lang=source_lang,
            target_lang=target_lang,
            input_type=input_type,
            file_filter=file_filter,
            tracks=tracks,
            suffix=suffix,
            output_format=output_format,
            cfg=cfg,
        )


if __name__ == "__main__":
    main()
