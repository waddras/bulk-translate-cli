#!/usr/bin/env python3
"""CLI entry point: argument parsing, help text, and dispatch.

SUBCOMMANDS
    btcli interactive — guided mode: pick track and styles per folder
    btcli probe       — inspect files for subtitle tracks, styles, tags
    btcli translate   — full translation pipeline
    btcli fix         — re-process translated files without API calls
    btcli prune       — report and reclaim stale cache and job records
    btcli update      — pull latest code, merge or repair settings

Verbosity flags are global and must appear BEFORE the subcommand:
    --quiet    minimal output (timestamps + summaries only)
    --verbose  full output (debug details, per-attempt logs)
    (default)  medium output (rich progress bars, colored, ETA)

WHY THIS FILE IS MOSTLY TEXT
    Roughly two thirds of it is help text held in ``*_EPILOG`` constants. That is
    deliberate: ``--help`` is the reference users actually reach for, so the
    epilogs carry full explanations and worked examples rather than one-line
    summaries. ``tests/test_docs.py`` checks that every subcommand, fix and
    documented flag really exists, so this text cannot drift away from the code.

    The no-command summary printed by a bare ``btcli`` is maintained separately
    from the argparse help and is also covered by those tests.

DISPATCH
    Every branch imports its flow lazily. Startup would otherwise pull in
    pysubs2, httpx and fonttools just to print ``--help``.

    Settings are validated before any command runs, so a mistake surfaces up
    front rather than as a confusing failure an hour into a job.
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
  btcli interactive                                   guided, asks per folder
  btcli probe -p "/media/anime/Show"                  see tracks + styles first
  btcli translate -p "/media/anime/Show" --auto       translate a whole folder
  btcli translate -p ep.mkv -s "ALL,+karaoke"         translate one file
  btcli fix -p "/media/anime/Show" --apply all        repair existing output

  Every command defaults -p to the CURRENT directory, and scanning never goes
  more than one directory deep (a series folder with season folders is covered).

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

RESUME AND THE TRANSLATION CACHE (.btcli-cache.json)
  Every translated line is written to a cache at the series root AS EACH API
  RESPONSE ARRIVES, not at the end. So an interrupted run, a crash, or a job
  that ends with files incomplete never loses the lines it already paid for.

  Re-running is therefore a resume: cached lines are reused and only missing
  lines are sent. A file that failed on 2 lines out of 500 costs 2 lines to
  finish, not 500. Keyed by source text, so renaming files, reordering cues, or
  re-extracting a track cannot corrupt it. Use --no-cache to translate fresh.

  When an EARLIER run's lines are found you are asked once whether to resume or
  start over, and the cache state is always reported in Phase 1 so it is never
  a mystery whether resuming is in effect. Lines this run just translated are
  reused without asking, and nothing is asked once translating has begun, so an
  unattended job cannot stall waiting for an answer. Declining re-translates the
  earlier lines only. Set RESUME_PROMPT false to always resume without asking;
  the question is skipped automatically when not run from a terminal.

  Seasons of one series share the cache, so repeated lines (openings, endings,
  catchphrases) are only ever translated once.

PARTIAL FILES
  A file missing PARTIAL_LINE_TOLERANCE lines or fewer (default 10) is written
  anyway, with those lines left in the source language and listed in the log.
  Missing more than that and the file is skipped instead. Either way the lines
  are recorded, and a later run finishes the file and rewrites it complete.

WHEN LINES ARE MISSING
  If any lines are still missing when a job ends, btcli reports them once,
  after every folder, and offers four choices:

    [Y] retry   re-send only the missing lines, at a smaller chunk size
                (50% by default, or any percentage or line count). One oversized
                request that fails takes down every line in it, scattering
                damage across many files, so smaller chunks recover far more.
                The chunk size applies to that session only and is never
                written to settings.conf.
    [s] show    list the untranslated lines, grouped by folder, so you can see
                whether they are worth another attempt. Returns to this menu.
    [p] passthrough
                write the files now with those lines left in the source
                language. Makes NO API calls: the files are assembled from
                cached lines. A later run finishes them and rewrites them
                complete.
    [n] nothing lines stay cached for a later run

  Retrying loops until it succeeds or you choose otherwise. If a retry recovers
  nothing you are returned to this menu rather than left stuck.

JOB MANIFEST (.btcli.json)
  Each media directory gets a hidden .btcli.json recording every btcli run as
  job1, job2, job3 ... with the tracks extracted, files written, chunk sizes,
  cue counts, model used and status.

  Subtitle extraction is SKIPPED when a previous job already extracted that
  video AND the extracted file still exists on disk and still matches the
  source. Use --force to ignore the manifest and re-extract.

RATE LIMITS AND MODELS
  GEMINI_MODEL is tried first, then every model in MODEL_POOL. The two are
  merged and de-duplicated into one ladder, so a pool that repeats the primary
  never wastes a retry on the model that just failed.

  EVERY model in the ladder is tried before a chunk is given up on. If
  RETRY_ATTEMPTS is lower than the number of models it is raised automatically,
  so no model is ever skipped. Set it higher to allow more passes.

  A failure moves to the next model immediately; the cooldown only applies once
  every model has been tried and the ladder starts repeating. A 429 therefore
  switches model at once instead of waiting on a model that is out of quota.

  Order the pool by quota: a model with 500 requests/day survives a long job,
  one with 20/day will start returning 429 partway through.

FONTS
  With EMBED_FONT true, an Arabic font subset is embedded into ASS output so
  any player can render it. Set it false if your player already has Arabic
  fonts (e.g. Jellyfin's fallback font path) to save ~200KB per file.

SEEING THE WORK FIRST
  A job can run for the better part of an hour and use a day's request
  allowance. --dry-run reports the shape of it and stops:

    btcli translate -p PATH --dry-run
    btcli interactive --dry-run

  It shows the cue count, how many lines survive deduplication and the cache,
  how many API requests that becomes, the estimated output tokens, and the
  minimum time the pacing alone will take. It sends nothing, writes no output,
  and records nothing in the manifest or cache, so it needs no API key.

  With video input it stops before extraction, since extraction writes files.
  Line counts need an extracted track, so extract once and then dry-run with
  -i sub for full numbers.

CHECKING YOUR SETTINGS
  settings.conf is validated before any work starts, at three levels:

    error    the job cannot run correctly. Always stops the run.
    warning  it will run, but not as intended. Stops it only with --strict.
    note     worth knowing, nothing to do. Never stops anything.

    btcli --check-settings        validate and exit, doing no work
    btcli --strict translate ...  refuse to run if anything looks wrong

  Warnings catch quiet mistakes rather than crashes, for example a MODEL_POOL
  that repeats a model, which shortens the retry ladder without saying so.
  A RETRY_ATTEMPTS lower than the number of models is only a note, because it is
  raised automatically and there is nothing for you to change.

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

  A single failed request loses every line in it. Big chunks therefore damage
  many files at once, which is why --files-per-call 1, or a smaller
  MAX_LINES_PER_CHUNK, recovers more when the API is unreliable.

RESUME, PARTIAL FILES, AND RETRY
  Translated lines are cached at the series root as each response arrives, so
  re-running this same command resumes instead of starting over: only missing
  lines are sent. Pass --no-cache to ignore the cache and translate fresh.

  Files missing 10 lines or fewer (PARTIAL_LINE_TOLERANCE) are written with
  those lines left in the source language and reported; more than that and the
  file is skipped. When the job ends with lines missing you can retry at a
  smaller chunk size, list the untranslated lines to judge them, or write the
  files anyway with those lines left as-is.

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
  font-strip  remove the embedded font, saving roughly 200KB per file. Use this
              once the player supplies Arabic fonts itself, for example through
              Jellyfin's fallback font path, so existing files can be slimmed
              without re-translating them
  style       re-apply font name/size/outline/margins from settings.conf
  all         rtl, linebreak, font and style

  font-strip is NOT part of "all", because it is the opposite of "font".
  Asking for both at once is refused rather than guessed at.

TARGETING FILES
  -f is a filename substring filter, default ".ar." so only translated files
  are touched. Point -p at a file or a directory.

EXAMPLES
  btcli fix -p "/media/anime/Show" --apply all --backup
  btcli fix -p "/media/tv/Show" -f ".ar.srt" --apply linebreak,rtl
  btcli fix -p ep.ar.ass --apply font,style
  btcli fix -p "/media/anime" --apply font-strip --backup
  btcli fix -p "/media/anime" -f ".ara." --apply rtl

Use --backup to write a .bak copy before overwriting. Files are edited in
place, so --backup is recommended the first time.
"""

INTERACTIVE_EPILOG = """\
Guided translation. Nothing is written or sent until you confirm the summary.

WHAT IT ASKS
  1. input type: vid (extract tracks from video) or sub (existing subtitles)
  2. path (press Enter for the current directory)
  3. which folders to translate, as a numbered list - skip whole seasons here
  4. whether Gemini should choose the track and styles, and on what instruction
  5. for each CHOSEN folder, it samples ONE file and asks:
       - which subtitle track to use (bitmap tracks are shown but rejected)
       - which styles to translate, as a numbered list
     With AI selection on, its choice is shown here for you to accept first.
  6. force re-extraction? (default no)
  7. files per API call? (default auto)
  8. a summary, then Proceed? [Y/n]

  When every folder has finished, any lines still missing are reported once,
  with the choice to retry at a smaller chunk size, list the untranslated lines,
  write the files anyway with those lines left in the source language, or leave
  them cached. Translated lines are already cached, so a retry only sends what
  is missing.

  Each folder keeps its OWN track and style choice, so a series whose seasons
  differ is handled in one pass.

GOING BACK
  Type b (or back) at ANY prompt to return to the previous question. Answers
  already given are kept, so stepping back and forward again does not make you
  retype them.

    at the style prompt      returns to the track prompt for that folder
    at the track prompt      returns to the previous folder
    at the first folder      returns to the AI selection question
    at the AI question       returns to the folder picker
    at the folder picker     returns to the path
    at force / files-per-call / the summary
                             returns one step back, and from force back into
                             the last folder's questions

  The summary also offers direct edits without stepping back through
  everything:

    [e] edit a folder   redo just that folder's track and styles
    [d] drop a folder   remove it from the plan entirely

  Nothing is sent or written until you choose to proceed.

CHOOSING FOLDERS
  Skip entire seasons before any track or style questions are asked.

    ALL       every folder                                        (default)
    1,3       only folders 1 and 3
    -3        every folder except 3
    2,4,5     only folders 2, 4 and 5

  Include and exclude numbers cannot be mixed. The prompt is skipped when only
  one folder was found.

STYLE SELECTION
  The numbered list comes from the sampled file. Numbers and style names may be
  mixed, and a leading + means passthrough (copied to the output untouched, not
  sent to the API).

    +ALL,1          passthrough all styles, translate only style 1
    1,2,+karaoke    translate styles 1 and 2, passthrough karaoke
    ALL,+karaoke    translate all styles, passthrough karaoke   (default)
    1,3,+ALL        translate styles 1 and 3, passthrough the rest
    +3              passthrough style 3

LETTING GEMINI CHOOSE
  Asked once, before the folder questions. Worth it on a release with forty
  styles (sign1..sign10, NodameOP, EdEnglish, letter1) where picking by hand is
  guesswork.

  It costs ONE extra API call per folder, on the model pinned by
  AI_SELECT_MODEL, so a selection never spends a translation model's daily
  quota. Each style is judged on its cue count, a few sample lines, and whether
  its cues carry \\pos (a sign) or \\k (karaoke) - names alone are a weak signal.

  The numbered style list is printed first, and it is the same numbering the
  model receives, so its answer can be read straight against it. The model
  replies with NUMBERS, not names, which rules out case slips, reformatted names
  and invented ones - a number is either offered or it is not.

    Track 0  [eng] ass  "English"
    1) Base01          2) Base01 - Overlap   3) EdEnglish     4) NodameOP
    5) NodameED        6) Nodame Primary     7) letter1       8) sign1
    Asking Gemini to choose the track and styles...
    gemini-3.5-flash-lite chose track 0 and 2 of 41 style(s):
      translate:   1) Base01, 6) Nodame Primary
      passthrough: the other 39 style(s), untouched
      reason:      Base01 and Nodame Primary carry hundreds of conversational
                   cues; the rest are positioned signs or karaoke.
    Use this selection? [Y/n]

  Nothing is taken on trust: a number that was not offered is discarded, a reply
  spanning two tracks keeps only the track most of its numbers belong to, and a
  reply with nothing valid left is dropped rather than widened to "translate
  everything". Answer n, or let the call fail, and you get the ordinary style
  prompt with nothing lost.

  The verdict is cached in that folder's .btcli.json and reused only while the
  styles on disk still match. A cached verdict still has to be confirmed, so
  nothing stale is ever used without you seeing it.

  AI_SELECT_STYLES sets the default answer; AI_SELECT_PROMPT is the default
  instruction. Edit that to change what counts as dialogue for your library.

WHEN A FOLDER HAS NO TRACKS
  If a video has no subtitle tracks but subtitle files sit beside it, you are
  asked whether to use those instead. Folders with neither are skipped.

NOTES
  Target language comes from settings.conf and is never prompted.
  Style sampling extracts one track to a temporary file, which is discarded.
  Ctrl-C before the summary cancels safely; nothing is written.
  Needs a real terminal - use 'btcli translate' in scripts.

EXAMPLES
  btcli interactive
  btcli -v interactive
  btcli interactive -p "/media/anime/Chihayafuru"
"""

PRUNE_EPILOG = """\
btcli leaves two state files beside your media, and both grow without bound:

  .btcli-cache.json   every line ever translated for a series
  .btcli.json         every job ever run in that folder

Neither is harmful for a long time, but this makes them inspectable and
trimmable. Running with no options reports sizes and what could be removed
WITHOUT changing anything.

WHAT GETS REMOVED
  cache      entries whose source line no longer appears in any subtitle in
             that folder tree, so they could never be reused from there. Lines
             still present are always kept, so resuming is unaffected.
  manifest   job history older than the most recent --keep jobs. Remaining jobs
             are renumbered so the history stays job1..jobN.

  Cache pruning is per language, since a cache holds each target language
  separately. Only the language given by -l, or TARGET_LANGUAGE, is touched.

EXAMPLES
  btcli prune                                 report on the current directory
  btcli prune -p /media/anime                 report on a whole library
  btcli prune -p /media/anime --apply         trim both
  btcli prune --what manifest --keep 3 --apply
  btcli prune --what cache -l french --apply

NOTES
  Trimming the cache does not lose translations you still need: an entry is only
  removed when nothing in the folder tree still contains that source line. If
  you have moved files elsewhere, prune from the parent so their lines are seen.
"""

UPDATE_EPILOG = """\
Update the installed copy of btcli.

WHAT IT DOES
  1. fetch origin, then fast-forward the current branch
  2. create settings.conf from settings.default.conf if you have none
  3. merge any NEW settings keys into your existing settings.conf,
     preserving your values and comments, and list deprecated keys

Your settings.conf is never overwritten; only missing keys are added.

FAST-FORWARD ONLY
  btcli never creates a merge commit in your install. If your checkout has
  local commits the remote does not, the update stops and tells you how to
  rebase manually. Nothing is rewritten behind your back.

LOCAL EDITS
  Uncommitted edits to tracked files block the update, because a fast-forward
  would clobber them. Either pass --stash (btcli sets them aside and puts them
  back afterwards) or discard them yourself.

  Untracked files never block anything, and settings.conf plus the runtime
  state files (.btcli.json, .btcli-cache.json) are gitignored, so your config
  and resume data always survive an update.

OPTIONS
  --check          report what an update would do, change nothing
  --branch NAME    switch to NAME and update that instead of the current branch
  --stash          set local edits aside, update, then restore them
  --reset KEYS     restore the shipped default for these settings
  --dedupe         remove repeated settings, keeping the value in effect

REPAIRING settings.conf
  Because your values are never overwritten, a setting can go stale: it keeps
  working while no longer matching what the code expects. --check-settings warns
  about the cases that matter, and two options fix them without touching
  anything else in the file. Both write a .bak first, and neither pulls code.

  --reset KEYS restores the shipped default. Use it when a value is simply
  wrong, such as a PROMPT_TEMPLATE still demanding a JSON object:

    btcli update --reset PROMPT_TEMPLATE
    btcli update --reset PROMPT_TEMPLATE,STRIP_TAGS

  Careful: it restores the DEFAULT, so it will discard a value you chose on
  purpose.

  --dedupe removes repeated declarations of the same setting. JSON silently
  keeps the last one, so a file can hold two values for one key and look fine.
  Dedupe keeps the one already in effect, so nothing changes behaviour — the
  file just stops disagreeing with itself:

    btcli update --dedupe

EXAMPLES
  btcli update                          # update the current branch
  btcli update --check                  # is there anything new?
  btcli update --branch main            # move back to the release branch
  btcli update --stash                  # update despite local edits
  btcli update --reset PROMPT_TEMPLATE  # restore one setting's default
  btcli update --dedupe                 # collapse repeated settings
  btcli --verbose update
"""


def _parse_args():
    """Build the parser and parse argv.

    Global flags sit on the top-level parser, so they must precede the
    subcommand. Each subparser carries a RawDescriptionHelpFormatter epilog,
    which is why the help text keeps its layout.
    """
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
    parser.add_argument("--strict", action="store_true",
                        help="Treat settings.conf warnings as fatal instead of continuing")
    parser.add_argument("--check-settings", action="store_true",
                        help="Validate settings.conf and exit without doing any work")

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
    p_probe.add_argument("-p", default=".", metavar="PATH",
                         help="File or directory to inspect. Default: current directory")
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
    p_trans.add_argument("-p", default=".", metavar="PATH",
                         help="File or directory to translate. Default: current directory")
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
    p_trans.add_argument("--no-cache", action="store_true",
                         help="Ignore the translation cache and re-translate every line")
    p_trans.add_argument("--dry-run", action="store_true",
                         help="Report the work, requests and estimated tokens, then stop "
                              "without sending, writing, or recording anything")

    # ── fix ───────────────────────────────────────────────────────────────────
    p_fix = sub.add_parser(
        "fix",
        help="Repair already-translated files without calling the API",
        description="Re-process existing translated subtitles: RTL wrapping, line breaks, "
                    "embedded fonts, and style settings. Makes no API calls.",
        epilog=FIX_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_fix.add_argument("-p", default=".", metavar="PATH",
                       help="File or directory to repair. Default: current directory")
    p_fix.add_argument("-f", default=".ar.", metavar="FILTER",
                       help="Only files whose name contains this substring. Default: '.ar.'")
    p_fix.add_argument("--apply", default="all", metavar="FIXES",
                       help="Fixes to apply: rtl, font, font-strip, style, linebreak, all (comma-separated). Default: all, which is every fix except font-strip")
    p_fix.add_argument("--backup", action="store_true",
                       help="Write a .bak copy before overwriting each file")

    # ── interactive ───────────────────────────────────────────────────────────
    p_inter = sub.add_parser(
        "interactive",
        help="Guided mode: pick track and styles per folder, then translate",
        description="Walk through each folder one level deep, choosing the subtitle "
                    "track and the styles to translate, then translate every folder "
                    "with its own settings.",
        epilog=INTERACTIVE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_inter.add_argument("-p", default=None, metavar="PATH",
                         help="Skip the path prompt and use this path. Default: ask, "
                              "offering the current directory")
    p_inter.add_argument("--dry-run", action="store_true",
                         help="Ask the same questions, then report the work and stop "
                              "without sending, writing, or recording anything")

    # ── prune ─────────────────────────────────────────────────────────────────
    p_prune = sub.add_parser(
        "prune",
        help="Inspect and trim the cache and job history btcli leaves beside media",
        description="Report how much space .btcli-cache.json and .btcli.json use, "
                    "and optionally trim them. Reports only unless --apply is given.",
        epilog=PRUNE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_prune.add_argument("-p", default=".", metavar="PATH",
                         help="Directory to search, at any depth. Default: current directory")
    p_prune.add_argument("--what", default="all", metavar="TARGETS",
                         help="What to trim: cache, manifest, all. Default: all")
    p_prune.add_argument("--keep", type=int, default=10, metavar="N",
                         help="Job history entries to keep per folder. Default: 10")
    p_prune.add_argument("--apply", action="store_true",
                         help="Actually make the changes. Without this, nothing is removed")
    p_prune.add_argument("-l", default=None, metavar="LANG",
                         help="Cache language to trim. Default: TARGET_LANGUAGE from settings")

    # ── update ────────────────────────────────────────────────────────────────
    p_update = sub.add_parser(
        "update",
        help="Pull the latest code and merge any new settings keys",
        description="Update the installed copy of btcli and add new settings keys to "
                    "your settings.conf without overwriting your values.",
        epilog=UPDATE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_update.add_argument("--check", action="store_true",
                          help="Report what an update would do, then stop. Changes nothing")
    p_update.add_argument("--branch", default=None, metavar="NAME",
                          help="Switch to NAME and update it. Default: the current branch")
    p_update.add_argument("--stash", action="store_true",
                          help="Set uncommitted edits aside, update, then restore them")
    p_update.add_argument("--reset", default=None, metavar="KEYS",
                          help="Restore the shipped default for these settings "
                               "(comma-separated), leaving the rest of your "
                               "settings.conf untouched. Pulls no code")
    p_update.add_argument("--dedupe", action="store_true",
                          help="Remove repeated settings, keeping the value already "
                               "in effect. Changes no behaviour. Pulls no code")

    return parser.parse_args()


def main():
    """Configure logging, validate settings, then dispatch to a flow.

    Order matters: verbosity is applied before anything can log, and settings are
    validated before any command runs. Errors always block; warnings block only
    under --strict.

    Exits 1 with the usage summary when no subcommand is given, so the shell sees
    a failure rather than a silent success.
    """
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

    # Validate settings before any work: a mistake here used to surface only as a
    # confusing runtime failure, or as silently degraded behaviour.
    from .validate import report_settings
    if args.check_settings:
        from .config import _settings_file
        log.info(f"Checking {_settings_file or 'built-in defaults'}")
        ok = report_settings(strict=args.strict, announce_ok=True)
        sys.exit(0 if ok else 1)

    if not report_settings(strict=args.strict):
        sys.exit(1)

    if not args.command:
        print("No command specified. Run 'btcli -h' for full help,")
        print("or 'btcli COMMAND -h' for details on a command.\n")
        print("Commands:")
        print("  interactive  Guided mode: pick track and styles per folder")
        print("  probe        Inspect subtitle tracks, styles, and tags (no API calls)")
        print("  translate    Translate subtitle files or video subtitle tracks")
        print("  fix          Repair already-translated files (no API calls)")
        print("  prune        Report and reclaim stale cache and job records")
        print("  update       Pull latest code + merge new settings\n")
        print("Common usage:")
        print("  btcli interactive [-p PATH] [--dry-run]")
        print("  btcli probe -p PATH [-i vid|sub] [-m sample|recursive] [-f FILTER] [-o tracks,styles,tags]")
        print("  btcli translate -p PATH [-l LANG] [-i vid|sub] [-f FILTER] [-t TRACKS]")
        print("                  [-s STYLES] [-suffix .ar] [-o srt] [--show-name NAME]")
        print("                  [--auto] [--force] [--files-per-call N]")
        print("                  [--dry-run] [--no-cache]")
        print("  btcli fix -p PATH [-f FILTER] [--apply rtl,font,style,linebreak,font-strip,all] [--backup]")
        print("  btcli prune [-p PATH] [--what cache,manifest,all] [--keep N] [--apply]")
        print("  btcli update [--check] [--branch NAME] [--stash]\n")
        print("Paths default to the current directory; scanning stops one directory deep.\n")
        print("See what would happen before spending any API quota:")
        print("  btcli translate -p PATH --dry-run\n")
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

        result = run_translate(
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
            use_cache=not args.no_cache,
            dry_run=args.dry_run,
        )

        # Offer to re-send only the missing lines, once, at the very end.
        if result and result.get("missing"):
            from .retry import offer_retry
            offer_retry([result], api_key="")

    elif args.command == "fix":
        from .fix import run_fix
        run_fix(
            path=args.p,
            filter_pattern=args.f,
            apply=args.apply,
            backup=args.backup,
        )

    elif args.command == "interactive":
        from .interactive import run_interactive
        run_interactive(path=args.p, dry_run=args.dry_run)

    elif args.command == "prune":
        from .prune import run_prune
        run_prune(path=args.p, what=args.what, keep=args.keep,
                  apply=args.apply, target_lang=args.l)

    elif args.command == "update":
        from .update import run_update
        reset_keys = ([k.strip() for k in args.reset.split(",") if k.strip()]
                      if args.reset else None)
        run_update(branch=args.branch, check=args.check, stash=args.stash,
                   reset=reset_keys, dedupe=args.dedupe)

    log.close()


if __name__ == "__main__":
    main()
