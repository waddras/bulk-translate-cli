#!/usr/bin/env python3
"""CLI entry point for bulk-translate-cli.

Subcommands:
    btcli probe     — inspect files for subtitle tracks, styles, tags
    btcli translate — full translation pipeline
    btcli fix       — re-process translated files without API calls
    btcli update    — pull latest code + merge new settings

Verbosity flags are global and must appear BEFORE the subcommand:
    --quiet    minimal output (timestamps + summaries only)
    --verbose  full output (debug details, per-attempt logs)
    (default)  medium output (rich progress bars, colored, ETA)
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .logger import log


# ── Help text ─────────────────────────────────────────────────────────────────

MAIN_EPILOG = """\
GLOBAL FLAGS MUST COME BEFORE THE SUBCOMMAND
  btcli --verbose --log-file /tmp/b.log translate -p FILE     correct
  btcli translate -p FILE --log-file /tmp/b.log              WRONG (rejected)

QUICK START
  btcli probe -p "/media/anime/Show"                  see tracks + styles first
  btcli translate -p "/media/anime/Show" --auto       translate a whole folder
  btcli translate -p ep.mkv -s "ALL,+karaoke"         translate one file
  btcli fix -p "/media/anime/Show" --apply all        repair existing output

HOW TRANSLATION WORKS
  Phase 0  discover files; extract subtitle tracks from video (ffmpeg)
  Phase 1  parse cues, strip blacklisted tags, deduplicate identical lines
  Phase 2  split unique lines into chunks (or whole files, see --files-per-call)
  Phase 3  send chunks to Gemini; write each output file as soon as ALL of its
           lines are translated
  Phase 4  finalize; report any file left incomplete

  A file is NEVER written with missing lines. If some lines cannot be
  translated after all retries, that file is skipped and reported instead.

RELIABILITY
  Every line is sent with an inline ID that travels inside the text. Replies
  must return that same ID, so a translation can never be attached to the wrong
  cue. Mismatched, duplicated, or missing IDs are rejected and retried
  individually rather than silently accepted.

JOB MANIFEST (.btcli.json)
  Each media directory gets a hidden .btcli.json recording every btcli run as
  job1, job2, job3 ... with the tracks extracted, files written, chunk sizes,
  cue counts, model used and status.

  Subtitle extraction is SKIPPED when a previous job already extracted that
  video AND the extracted file still exists on disk and still matches the
  source. Use --force to ignore the manifest and re-extract.

RATE LIMITS AND MODELS
  GEMINI_MODEL is tried first; MODEL_POOL is the retry ladder. Put your
  highest-quota models first: a model with 500 requests/day survives a long
  job, one with 20/day will start returning 429 partway through.

FONTS
  With EMBED_FONT true, an Arabic font subset is embedded into ASS output so
  any player can render it. Set it false if your player already has Arabic
  fonts (e.g. Jellyfin's fallback font path) to save ~200KB per file.

CONFIGURATION (first match wins)
  ./settings.conf                     current directory (careful: takes priority)
  /opt/btcli/settings.conf            normal location
  ~/.config/btcli/settings.conf
  /etc/btcli/settings.conf
  API key: ~/.btcli.env or the GEMINI_API_KEY environment variable

  Edit settings.conf, never settings.default.conf (overwritten on update).

Run 'btcli SUBCOMMAND -h' for full details, e.g. 'btcli translate -h'.
"""

TRANSLATE_EPILOG = """\
STYLE SELECTION WITH -s  (ASS/SSA only; SRT has no styles)
  Bare name      translate this style          -s "Default"
  +name          PASS THROUGH untouched        -s "+Signs"
  ALL            translate every style         -s "ALL"
  +ALL           pass through every style      -s "+ALL"
  +karaoke       auto-detect karaoke styles and pass them through

  Passthrough means the original lines are copied to the output exactly as they
  are: same text, font, spacing and tags. Nothing is sent to the API. Use it
  for signs, songs and karaoke you want left in the source language.

  HOW TO SKIP KARAOKE (most common request):
    -s "ALL,+karaoke"          translate dialogue, leave karaoke as-is
    --auto                     same thing (this is the default for --auto)

  Karaoke is detected automatically: styles whose events are mostly
  single-character text or carry an [fx] effect field.

  More examples:
    -s "Default,Default-Alt,+OP-EN,+ED-EN,+Signs"
    -s "ALL,+OP-EN,+OP-RO,+karaoke"
    -s "+ALL"                  translate nothing, just restyle/copy

  Without -s, the top styles by unique line count are chosen automatically
  (KEEP_TOP_STYLES in settings.conf, default 2).

TRACKS WITH -t  (only with -i vid)
  Track numbers are 0-BASED and count only subtitle streams, as shown by
  'btcli probe'. -t 0 is the first subtitle track.
    -t 0        first subtitle track
    -t 3        fourth subtitle track
    -t 2,3      extract both and merge them into one file
  Bitmap tracks (PGS/DVD/DVB) cannot be translated and are skipped
  automatically; the first text track is used instead.

LANGUAGES WITH -l
  -l arabic              english -> arabic (source from SOURCE_LANGUAGE)
  -l japanese,english    explicit source,target pair
  The output suffix is derived from the target language (arabic -> .ar) unless
  you override it with -suffix.

OUTPUT
  ASS source -> ASS output, SRT source -> SRT output. Use -o srt to force SRT.
  ASS keeps positioning tags and uses \\N line breaks; SRT uses real newlines.
  Existing files are overwritten or renamed per FILE_CONFLICT in settings.conf.

CHUNKING
  By default unique lines are split by MAX_LINES_PER_CHUNK. With
  --files-per-call N, chunks follow FILE boundaries instead: N whole subtitle
  files per API call, ignoring MAX_LINES_PER_CHUNK entirely.
    --files-per-call 1     one file per request (best isolation, most requests)
    --files-per-call 2     two files per request
  Large batches are also grouped by FILES_PER_BATCH (default 25) and refuse to
  run if a batch exceeds MAX_BLOB_LINES.

EXAMPLES
  btcli translate -p "/media/anime/Show" --auto
  btcli translate -p "/media/tv/Show/Season 1" -s "ALL,+karaoke" -t 0
  btcli translate -p ep.mkv -s "Default,+Signs,+karaoke" -l arabic
  btcli translate -p "." -i sub -f ".en.ass" -s ALL
  btcli translate -p "/media/tv/Show" --files-per-call 1 --force
  btcli --verbose --log-file /tmp/b.log translate -p ep.mkv

NOTES
  Re-running does NOT skip translation, only extraction (see --force). Each run
  is appended to .btcli.json in every directory it touches.
"""

PROBE_EPILOG = """\
WHAT PROBE SHOWS
  tracks   every subtitle track: index, codec, language, title
  styles   ASS style names present in each track (use these with -s)
  tags     ASS override tags used (helps choose STRIP_TAGS)

  Probe makes no API calls and writes nothing. Run it before translate to learn
  the track numbers and style names you need.

SCAN MODES WITH -m
  sample      one file per subdirectory (fast survey of a series)
  recursive   every matching file

EXAMPLES
  btcli probe -p "/media/anime/Show"
  btcli probe -p "/media/anime/Show" -m recursive
  btcli probe -p ep.mkv -o tracks,styles,tags
  btcli probe -p "." -i sub -f ".en.ass"
  btcli --verbose probe -p ep.mkv

READING THE OUTPUT
  Track numbers are 0-based and are what you pass to 'translate -t'.
  Codecs hdmv_pgs_subtitle / dvd_subtitle are bitmap images, not text, and
  cannot be translated. Style names listed per track are what -s accepts.
"""

FIX_EPILOG = """\
Re-process files that were already translated. No API calls, no cost.

FIXES AVAILABLE WITH --apply
  rtl         re-wrap each line with RTL isolate marks so mixed
              Arabic/Latin/punctuation and line order display correctly
  linebreak   ASS: convert real newlines to \\N
              SRT: split literal \\N back into real lines (repairs two-line
              cues that were merged onto one line and appeared reversed)
  font        re-embed the Arabic font subset in ASS using ASS UUEncode
  style       re-apply font name/size/outline/margins from settings.conf
  all         every fix above (default)

TARGETING FILES
  -f is a filename substring filter, default ".ar." so only translated files
  are touched. Point -p at a file or a directory.

EXAMPLES
  btcli fix -p "/media/anime/Show" --apply all --backup
  btcli fix -p "/media/tv/Show" -f ".ar.srt" --apply linebreak,rtl
  btcli fix -p ep.ar.ass --apply font,style
  btcli fix -p "/media/anime" -f ".ara." --apply rtl

Use --backup to write a .bak copy before overwriting. Files are edited in
place, so --backup is recommended the first time.
"""

UPDATE_EPILOG = """\
Update the installed copy of btcli.

WHAT IT DOES
  1. git pull in the install directory (/opt/btcli)
  2. create settings.conf from settings.default.conf if you have none
  3. merge any NEW settings keys into your existing settings.conf,
     preserving your values and comments, and list deprecated keys

Your settings.conf is never overwritten; only missing keys are added.

REQUIREMENTS
  The install directory must be a clean git checkout. If you have local edits
  git pull will refuse — stash or discard them first.

EXAMPLES
  btcli update
  btcli --verbose update
"""


def _parse_args():
    parser = argparse.ArgumentParser(
        prog="btcli",
        description="Bulk subtitle translation CLI — translate SRT/ASS subtitles "
                    "or subtitle tracks inside video files using the Gemini API.",
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"btcli {__version__}")

    # Global verbosity flags
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", "-q", action="store_true",
                           help="Minimal output: timestamps + phase summaries only")
    verbosity.add_argument("--verbose", "-v", action="store_true",
                           help="Full output: debug details, per-attempt logs, response diagnostics")

    # Log file
    parser.add_argument("--log-file", default=None, metavar="PATH",
                        help="Write all output to a log file, at full detail regardless of verbosity")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND",
                                help="Command to run (see 'btcli COMMAND -h')")

    # ── probe ─────────────────────────────────────────────────────────────────
    p_probe = sub.add_parser(
        "probe",
        help="Inspect files for subtitle tracks, styles, and tags (no API calls)",
        description="Inspect subtitle tracks, ASS style names, and ASS override tags. "
                    "Makes no API calls and writes no files.",
        epilog=PROBE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_probe.add_argument("-p", required=True, metavar="PATH",
                         help="File or directory to inspect")
    p_probe.add_argument("-i", default="vid", choices=["vid", "sub"],
                         help="Input type: vid (video files) or sub (subtitle files). Default: vid")
    p_probe.add_argument("-m", default="sample", choices=["sample", "recursive"],
                         help="Scan mode: sample (one file per subdirectory) or recursive (all). Default: sample")
    p_probe.add_argument("-f", default=None, metavar="FILTER",
                         help="Only files whose name contains this substring (e.g. '.en.ass')")
    p_probe.add_argument("-o", default="tracks,styles", metavar="OUTPUTS",
                         help="What to report: tracks, styles, tags (comma-separated). Default: tracks,styles")

    # ── translate ─────────────────────────────────────────────────────────────
    p_trans = sub.add_parser(
        "translate",
        help="Translate subtitle files or video subtitle tracks (full pipeline)",
        description="Extract, translate, and write subtitle files. Each output file is "
                    "written as soon as all of its lines are translated; files with "
                    "missing lines are reported instead of being written.",
        epilog=TRANSLATE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_trans.add_argument("-p", required=True, metavar="PATH",
                         help="File or directory to translate")
    p_trans.add_argument("-l", default="arabic", metavar="LANG",
                         help="Target language, or 'source,target' pair (e.g. 'japanese,english'). Default: arabic")
    p_trans.add_argument("-i", default="vid", choices=["vid", "sub"],
                         help="Input type: vid (extract tracks from video) or sub (existing subtitle files). Default: vid")
    p_trans.add_argument("-f", default=None, metavar="FILTER",
                         help="Only files whose name contains this substring (e.g. '.en.ass')")
    p_trans.add_argument("-t", default="0", metavar="TRACKS",
                         help="Subtitle track number(s), 0-based, comma-separated to merge (only with -i vid). Default: 0")
    p_trans.add_argument("-s", "--styles", default=None, metavar="STYLES",
                         help="Style selection: 'Name' translates, '+Name' passes through untouched, "
                              "plus ALL / +ALL / +karaoke. Example: 'ALL,+karaoke' translates dialogue "
                              "and leaves karaoke as-is. See the examples below for the full syntax.")
    p_trans.add_argument("-suffix", default=None, metavar="SUFFIX",
                         help="Output filename suffix. Default: derived from target language (arabic -> .ar)")
    p_trans.add_argument("-o", default=None, choices=["srt"],
                         help="Force output format to SRT even when the source is ASS")
    p_trans.add_argument("--show-name", default="", metavar="NAME",
                         help="Override the auto-detected show name used for translation context")
    p_trans.add_argument("--auto", action="store_true",
                         help="Auto-detect the best subtitle track and use styles 'ALL,+karaoke'")
    p_trans.add_argument("--force", action="store_true",
                         help="Ignore the .btcli.json manifest and re-extract subtitles even if already extracted")
    p_trans.add_argument("--files-per-call", "-fpc", type=int, default=None, metavar="N",
                         help="Send N whole subtitle files per API call, ignoring MAX_LINES_PER_CHUNK")

    # ── fix ───────────────────────────────────────────────────────────────────
    p_fix = sub.add_parser(
        "fix",
        help="Repair already-translated files without calling the API",
        description="Re-process existing translated subtitles: RTL wrapping, line breaks, "
                    "embedded fonts, and style settings. Makes no API calls.",
        epilog=FIX_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_fix.add_argument("-p", required=True, metavar="PATH",
                       help="File or directory to repair")
    p_fix.add_argument("-f", default=".ar.", metavar="FILTER",
                       help="Only files whose name contains this substring. Default: '.ar.'")
    p_fix.add_argument("--apply", default="all", metavar="FIXES",
                       help="Fixes to apply: rtl, font, style, linebreak, all (comma-separated). Default: all")
    p_fix.add_argument("--backup", action="store_true",
                       help="Write a .bak copy before overwriting each file")

    # ── update ────────────────────────────────────────────────────────────────
    sub.add_parser(
        "update",
        help="Pull the latest code and merge any new settings keys",
        description="Update the installed copy of btcli and add new settings keys to "
                    "your settings.conf without overwriting your values.",
        epilog=UPDATE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

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
        print("No command specified. Run 'btcli -h' for full help,")
        print("or 'btcli COMMAND -h' for details on a command.\n")
        print("Commands:")
        print("  probe      Inspect subtitle tracks, styles, and tags (no API calls)")
        print("  translate  Translate subtitle files or video subtitle tracks")
        print("  fix        Repair already-translated files (no API calls)")
        print("  update     Pull latest code + merge new settings\n")
        print("Common usage:")
        print("  btcli probe -p PATH [-i vid|sub] [-m sample|recursive] [-f FILTER] [-o tracks,styles,tags]")
        print("  btcli translate -p PATH [-l LANG] [-i vid|sub] [-f FILTER] [-t TRACKS]")
        print("                  [-s STYLES] [-suffix .ar] [-o srt] [--show-name NAME]")
        print("                  [--auto] [--force] [--files-per-call N]")
        print("  btcli fix -p PATH [-f FILTER] [--apply rtl,font,style,linebreak,all] [--backup]")
        print("  btcli update\n")
        print("Skip karaoke while translating dialogue:")
        print("  btcli translate -p PATH -s \"ALL,+karaoke\"")
        print("  btcli translate -p PATH --auto\n")
        print("Verbosity (must come BEFORE the command):")
        print("  --quiet / -q     Minimal output (timestamps + summaries)")
        print("  --verbose / -v   Full output (debug details, response diagnostics)")
        print("  (default)        Medium output (progress bars, colors, ETA)")
        print("  --log-file PATH  Write full detail to a file")
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
        keep_styles = None
        passthrough_styles = None
        if args.styles:
            from .styles import parse_styles_arg
            keep_styles, passthrough_styles = parse_styles_arg(args.styles)
        elif args.auto:
            # Auto mode default: ALL,+karaoke
            from .styles import parse_styles_arg
            keep_styles, passthrough_styles = parse_styles_arg("ALL,+karaoke")

        # Auto track detection
        auto_track = None
        if args.auto and args.t == "0" and args.i == "vid":
            auto_track = True

        run_translate(
            path=args.p,
            lang=args.l,
            input_type=args.i,
            filter_pattern=args.f,
            track_indices=track_indices,
            suffix=args.suffix,
            force_srt=(args.o == "srt"),
            show_name=args.show_name,
            keep_styles=keep_styles,
            passthrough_styles=passthrough_styles,
            auto_track=auto_track,
            force=args.force,
            files_per_call=args.files_per_call,
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
