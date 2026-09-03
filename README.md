# bulk-translate-cli

Bulk subtitle translation from the command line, using the Gemini API. Point it
at a season folder and it extracts, translates, and writes subtitles for every
episode.

Command-line counterpart to [bulk-translate](https://github.com/waddras/bulk-translate)
(web UI version).

```bash
btcli interactive                          # guided: it asks, you answer
btcli translate -p /media/Show --dry-run   # what would this cost?
btcli translate -p /media/Show             # do it
```

## Contents

- [Install](#install)
- [First run](#first-run)
- [Commands](#commands)
- [How a translation runs](#how-a-translation-runs)
- [Resuming and the cache](#resuming-and-the-cache)
- [The job manifest](#the-job-manifest)
- [Selecting styles](#selecting-styles)
- [Translation modes](#translation-modes)
- [Settings reference](#settings-reference)
- [Housekeeping](#housekeeping)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Install

Requires Python 3.9+, and `ffmpeg`/`ffprobe` on PATH for anything involving
video files.

**As a self-updating checkout** (recommended — `btcli update` works):

```bash
sudo git clone https://github.com/waddras/bulk-translate-cli /opt/btcli
sudo pip install -r /opt/btcli/requirements.txt
echo 'alias btcli="PYTHONPATH=/opt/btcli python3 -m btcli"' >> ~/.bashrc
```

**With pip:**

```bash
pip install .
```

This puts a `btcli` command on your PATH. `btcli update` will not work with a
pip install — use `pip install --upgrade` instead.

## First run

btcli needs a Gemini API key. On first run it offers to set one up, writing it
to `~/.btcli.env`. You can also do it yourself:

```bash
echo 'GEMINI_API_KEY=your-key-here' > ~/.btcli.env
chmod 600 ~/.btcli.env
```

The `GEMINI_API_KEY` environment variable overrides the file.

Settings come from the first file found:

1. `./settings.conf` — working directory
2. `/opt/btcli/settings.conf` — install directory
3. `~/.config/btcli/settings.conf`
4. `/etc/btcli/settings.conf`

Anything you do not set falls back to a built-in default, so a partial
`settings.conf` is fine. Copy `settings.default.conf` to `settings.conf` to
start from the documented full set — and **edit `settings.conf`, never
`settings.default.conf`**, which `btcli update` overwrites.

Check your config before a long run:

```bash
btcli --check-settings           # validate and report
btcli --strict --check-settings  # treat warnings as errors too
```

Findings come at three levels. **Errors** always stop the run. **Warnings** mean
it will run but not as intended, and stop it under `--strict`. **Notes** are
worth knowing but need no action — a setting the code already corrects by itself
— so they never block and never withhold the all-clear.

## Commands

Every command takes `-p PATH` and defaults to the current directory. Run
`btcli COMMAND -h` for the full detail on any of them — the help is the
authoritative reference.

### `interactive` — guided mode

Asks which folder, which subtitle track, and which styles, showing what it
found at each step. Answer `b` at any prompt to go back a step. This is the
easiest way to start, and the best way to handle a series whose seasons were
subtitled differently.

```bash
btcli interactive
btcli interactive -p /media/Show
btcli interactive --dry-run     # ask everything, then report instead of running
```

#### Letting Gemini pick the track and styles

Interactive mode offers to choose for you. Useful on releases with forty styles
(`sign1`–`sign10`, `NodameOP`, `EdEnglish`, `letter1`, `gyabo`) where picking by
hand is guesswork.

```
Let Gemini choose the track and styles for you? [y/N]: y
Instructions for Gemini [Enter for the default]:

  FOLDER - Season 01  (12 file(s))
  Track 0  [eng] ass  "Signs & Songs"  (forced)
  1) sign1           2) NodameOP
  Track 1  [eng] ass  "Full Subtitles"
  1) Base01          2) Base01 - Overlap   3) EdEnglish      4) Nodame Primary
  Asking Gemini to choose the track and styles...
  gemini-3.5-flash-lite chose track 1, and 2 of 41 style(s):
    translate:   1) Base01, 4) Nodame Primary
    passthrough: the other 39 style(s), untouched
    reason:      Track 1 is the full subtitle track; Base01 and Nodame Primary
                 carry hundreds of conversational cues.
  Use this selection? [Y/n]
```

**The track is the first decision**, and the more consequential one: pick "Signs
& Songs" over "Full Subtitles" and every style choice after it is irrelevant. So
every text track is offered, with its ffprobe metadata passed through whole —
tags and disposition included, since `forced=1` is the clearest "signs only"
marker there is. A plain-text track with no ASS styles is offered too; choosing
it just means translating all of it.

Style numbers **restart at 1 for each track**, so they only mean anything
together with the track named, and cannot contradict it. The model replies with
**numbers, not names**, which removes a whole class of errors: no case slips, no
reformatted `Nodame Insert JP`, no invented `MainDialogue`.

The list you see is printed from the same numbering the model receives, so its
answer reads straight against it. Cue counts, `\pos`/`\k` flags and sample lines
go in the request but stay out of that list — they are what the model judges
styles on, and clutter for a human checking the result.

One extra API call per folder, on the model pinned by `AI_SELECT_MODEL` so it
never spends a translation model's daily quota.

Nothing is taken on trust: a track index that does not exist is refused outright,
a style number out of range for the chosen track is dropped, and a reply with
nothing valid left is discarded rather than widened to "translate everything".
Answer `n`, or let the call fail, and you get the normal style prompt with
nothing lost.

The verdict is cached in that folder's `.btcli.json` and reused only while the
styles on disk still match, so a re-release with renamed styles gets a fresh
one. A cached verdict still has to be confirmed — it saves the call, not the
decision — so nothing stale can be used without you seeing it. Edit
`AI_SELECT_PROMPT` to change what counts as dialogue for your library.

### `probe` — look, don't touch

Reports subtitle tracks, style names, and ASS override tags. Makes no API
calls and writes nothing.

```bash
btcli probe -p /media/Show -i vid              # tracks, one file per subfolder
btcli probe -p /media/Show -i vid -m recursive # every file
btcli probe -p /media/Show -i sub -o styles,tags
btcli probe -p /media/Show -i sub -f ".en.ass" # only matching filenames
```

### `translate` — the main event

```bash
# Subtitle tracks inside video files (default: track 0, english -> arabic)
btcli translate -p /media/Show

# Existing subtitle files instead of video
btcli translate -p /media/Show -i sub -f ".en.ass"

# Merge several tracks (dialogue + signs)
btcli translate -p /media/Show -i vid -t 0,2

# Other languages
btcli translate -p /media/Show -l french
btcli translate -p /media/Show -l "japanese,english"

# Let btcli pick the track and skip karaoke
btcli translate -p /media/Show --auto

# Output shape
btcli translate -p /media/Show -o srt -suffix ".ar"
```

Useful flags:

| Flag | What it does |
|------|--------------|
| `--dry-run` | Report the work, request count and estimated tokens, then stop. Sends nothing, writes nothing, records nothing |
| `--auto` | Detect the best subtitle track and use styles `ALL,+karaoke` |
| `--files-per-call N`, `-fpc N` | Send N whole files per request instead of splitting by `MAX_LINES_PER_CHUNK`. Fewer requests, larger ones |
| `--no-cache` | Ignore cached translations and re-translate everything |
| `--force` | Ignore the manifest and re-extract subtitles that were already extracted |
| `--show-name NAME` | Override the auto-detected show name sent as translation context |

Always worth doing first on a new series:

```bash
btcli translate -p /media/Show --dry-run
```

It tells you how many files, cues, and API requests are involved, and what is
already cached, before any quota is spent.

### `fix` — repair output without the API

Re-processes files btcli already translated. No API calls.

```bash
btcli fix -p /media/Show                          # all fixes
btcli fix -p /media/Show --apply rtl,linebreak
btcli fix -p /media/Show --apply font-strip --backup
```

| Fix | What it does |
|-----|--------------|
| `rtl` | Re-apply bidi marks so RTL text renders in the right order |
| `font` | Embed a subsetted font in the ASS file |
| `font-strip` | Remove an embedded font, shrinking the file |
| `style` | Re-apply the `FONT_*` settings to the styles |
| `linebreak` | Fix line breaks inside cues |
| `all` | Every fix **except** `font-strip`, which is the opposite of `font` |

`--backup` writes a `.bak` beside each file before overwriting it.

### `prune` — reclaim state

See [Housekeeping](#housekeeping).

### `update` — pull the latest version

See [Updating](#updating).

## How a translation runs

1. **Discover** — find files under the path, skipping `SKIP_DIRS`
2. **Extract** — with `-i vid`, pull the requested tracks out with ffmpeg,
   merging them if you asked for several. Already-extracted files are reused
3. **Parse** — load the SRT/ASS, clean the text, remember positioning tags
4. **Blob** — deduplicate: identical lines are translated once and fanned back
   out to every cue that used them
5. **Chunk** — split into requests of at most `MAX_LINES_PER_CHUNK` cues
   (or whole files, with `--files-per-call`)
6. **Translate** — send each chunk to Gemini. Each line carries an explicit ID,
   and the reply is validated against those IDs, so a line can never land on the
   wrong cue
7. **Retry** — a failure switches to the next model immediately. Missing lines
   are re-sent with surrounding context for up to `MAX_FAILED_CHUNKS` rounds
8. **Write** — output files get bidi marks, line breaks, styles, and optionally
   an embedded font

Results are written as they arrive, not at the end, so an interrupted run keeps
everything it had already translated.

### Every model gets a turn

`GEMINI_MODEL` and `MODEL_POOL` are merged and de-duplicated into one ladder.
When a request fails, btcli moves to the next model straight away rather than
waiting — the cooldown applies only once the whole ladder has been tried. If
`RETRY_ATTEMPTS` is lower than the number of distinct models, it is raised so
no model is skipped.

Order `MODEL_POOL` by quota, highest daily limit first.

## Resuming and the cache

Every translated line is recorded in `.btcli-cache.json` at the series root as
each response arrives. The cache is keyed by **source text**, so renaming or
reordering files cannot corrupt it.

A re-run therefore only sends what is still missing. Interrupt a job with
Ctrl-C, or lose it to a rate limit, and starting it again picks up where it
stopped.

When lines cached by an **earlier** run are found, btcli asks once whether to
resume or start fresh. It never asks about lines the current run translated
minutes ago — seasons share a cache, so season 1 fills it and season 2 would
otherwise be interrupted to ask permission to reuse the same run's own work. Nor
does it ask once chunks have started going out: a job already under way silently
resumes rather than stalling on a keypress nobody is there to press.

Declining re-translates the lines from earlier runs and keeps what the current
run has already paid for. Set `RESUME_PROMPT` to `false` to always resume
silently; the prompt is skipped automatically when not running in a terminal.
`USE_TRANSLATION_CACHE` turns the whole mechanism off, as does `--no-cache` for a
single run.

### When a few lines refuse to translate

`PARTIAL_LINE_TOLERANCE` (default 10) is how many unique lines may still be
missing and have the file written anyway, with those lines left in the source
language and reported. Above that, the file is skipped. Set it to `0` for
all-or-nothing.

When lines are still missing after the retry rounds, btcli offers to retry
them, write the file with those lines passed through untranslated, or list them
so you can see what it is stuck on.

## The job manifest

Each folder btcli works in gets a `.btcli.json` recording the jobs run there:
the track and styles you chose, and which subtitles were extracted.

This is what lets a re-run skip re-extracting subtitles it already has — it
checks the manifest *and* confirms the file is still on disk, so deleting an
extracted file makes btcli extract it again. `--force` ignores the manifest
entirely.

Both `.btcli.json` and `.btcli-cache.json` are runtime state living next to
your media, not in the repo.

## Selecting styles

ASS files carry named styles: dialogue, signs, karaoke, opening credits.
`-s`/`--styles` decides what happens to each.

| Syntax | Meaning |
|--------|---------|
| `Default` | Translate the `Default` style |
| `+Signs` | Pass `Signs` through untouched |
| `ALL` | Translate every style |
| `+ALL` | Pass everything through |
| `+karaoke` | Pass through any style that looks like karaoke |

Combine them:

```bash
btcli translate -p /media/Show -s "ALL,+karaoke"   # dialogue yes, karaoke as-is
btcli translate -p /media/Show -s "Default,+Signs"
btcli translate -p /media/Show --auto              # same as ALL,+karaoke
```

Without `-s`, `KEEP_TOP_STYLES` (default 2) keeps the styles with the most
unique lines and drops the rest. Set it to `0` to keep everything.

Not sure what a file contains? `btcli probe -p PATH -i sub -o styles`.

## Translation modes

| Mode | Behaviour |
|------|-----------|
| `chunked` | Independent chunks, batched with a cooldown. Fastest and cheapest. Default |
| `multi_turn` | The full blob is context; chunks become conversation turns |
| `full_context` | The full blob is sent every request, with only specific keys translated. Best consistency, most tokens |

Set with `TRANSLATION_MODE`.

## Settings reference

Full documentation with comments lives in `settings.default.conf`. The defaults
below are what btcli uses if you configure nothing.

### API

| Setting | Default | Description |
|---------|---------|-------------|
| `GEMINI_API_KEY_FILE` | `~/.btcli.env` | Where the key is read from |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | First model tried |
| `MODEL_POOL` | 4 flash models | Model ladder, merged with `GEMINI_MODEL` |
| `GEMINI_MAX_OUTPUT_TOKENS` | `0` | `0` = let the model decide |
| `GEMINI_RESPONSE_SCHEMA` | `true` | Pin the reply to `[{id, text}]`. Disable only if a model rejects schemas |

### AI style selection (interactive mode only)

| Setting | Default | Description |
|---------|---------|-------------|
| `AI_SELECT_STYLES` | `false` | Default answer to "let Gemini choose the track and styles?". You are asked either way |
| `AI_SELECT_MODEL` | `gemini-3.5-flash-lite` | Model for that one call per folder. Pinned, so it never spends a translation model's quota |
| `AI_SELECT_PROMPT` | see file | What counts as dialogue. The reply format is appended automatically and overrides it |

### Translation

| Setting | Default | Description |
|---------|---------|-------------|
| `TRANSLATION_MODE` | `chunked` | `chunked`, `multi_turn`, `full_context` |
| `MAX_LINES_PER_CHUNK` | `1000` | Cues per request |
| `FILES_PER_BATCH` | `25` | Files deduplicated and chunked together. Lower it if a run trips `MAX_BLOB_LINES` |
| `PARALLEL_CHUNKS` | `1` | Requests fired together. Raise only with headroom on RPM |
| `PARALLEL_COOLDOWN` | `60` | Seconds between requests (between batches when parallel) |
| `RETRY_ATTEMPTS` | `5` | Attempts per chunk. Raised to fit the model ladder |
| `RETRY_COOLDOWN` | `10` | Seconds between retries, multiplied by attempt number |
| `MAX_FAILED_CHUNKS` | `5` | Retry rounds for lines still missing at the end |
| `MAX_BLOB_LINES` | `50000` | Safety cap on total cues per run |
| `USE_TRANSLATION_CACHE` | `true` | Record translations for resuming |
| `RESUME_PROMPT` | `true` | Ask before reusing a cache |
| `PARTIAL_LINE_TOLERANCE` | `10` | Missing unique lines tolerated before skipping a file |
| `PROMPT_TEMPLATE` | see file | The prompt. Must **not** specify a reply shape — the output contract is appended automatically |

### Languages

| Setting | Default | Description |
|---------|---------|-------------|
| `SOURCE_LANGUAGE` | `english` | Used when `-l` gives only a target |
| `TARGET_LANGUAGE` | `arabic` | Used when `-l` is omitted |
| `LANGUAGE_CODES` | 10 languages | Language name to filename suffix (`arabic` → `.ar`) |

### Output and fonts

| Setting | Default | Description |
|---------|---------|-------------|
| `FILE_CONFLICT` | `overwrite` | `overwrite` or `rename` (adds `_1`, `_2`) |
| `EMBED_FONT` | `true` | Embed a subsetted font in ASS output |
| `FONT_NAME` | `Noto Sans Arabic` | Font for the most prominent style |
| `FONT_NAME_SECONDARY` | `""` | Font for other kept styles. Empty = use `FONT_NAME` |
| `FONT_SIZE` | `18` | Font size for generated styles |
| `FONT_OUTLINE` | `1` | Outline thickness |
| `FONT_SHADOW` | `0` | Shadow depth |
| `FONT_ALIGNMENT` | `2` | SSA numbering: `2` = bottom-centre, `8` = top-centre |
| `FONT_MARGIN_L` | `20` | Left margin in pixels |
| `FONT_MARGIN_R` | `20` | Right margin in pixels |
| `FONT_MARGIN_V` | `30` | Vertical margin in pixels |

### Tags and discovery

| Setting | Default | Description |
|---------|---------|-------------|
| `STRIP_TAGS` | `fn, fs, fsp, b, i, u, s;` | ASS tags removed from output. Everything else is kept |
| `PRESERVE_TAGS` | `pos, an, move, fad, fade;` | Legacy whitelist; `STRIP_TAGS` takes priority |
| `KEEP_TOP_STYLES` | `2` | Styles to keep by unique line count. `0` = all. `-s` overrides |
| `SOURCE_EXTENSIONS` | `.srt .ass .ssa` | Looked for with `-i sub` |
| `MKV_EXTENSIONS` | `.mkv .mp4 .avi` | Looked for with `-i vid` |
| `SKIP_DIRS` | `Extras`, `Featurettes`, … | Folder names ignored while discovering. Case-sensitive |

## Housekeeping

Cache and manifest files grow as you translate more. `prune` reports what could
be reclaimed and, with `--apply`, reclaims it.

```bash
btcli prune -p /media                         # report only, changes nothing
btcli prune -p /media --apply                 # do it
btcli prune --what manifest --keep 3 --apply  # trim job history
btcli prune --what cache -l french --apply    # only the french cache
```

Nothing is removed without `--apply`.

Cache entries go only when **the source line no longer exists on disk**, so
pruning never throws away work you could still resume from — it drops
translations of subtitles you have deleted. Manifest job history is trimmed
oldest-first past `--keep` and the remaining jobs are renumbered.

## Updating

```bash
btcli update --check     # is there anything new?
btcli update             # fetch and fast-forward, then merge new settings keys
```

`update` fast-forwards only. If your checkout has local commits, it stops and
tells you how to rebase rather than rewriting anything.

Your `settings.conf` is never overwritten. New keys from
`settings.default.conf` are added with their defaults, your values and comments
are left alone, and keys btcli no longer uses are reported rather than deleted.

| Flag | What it does |
|------|--------------|
| `--check` | Report what an update would do, change nothing |
| `--branch NAME` | Switch to `NAME` and update that |
| `--stash` | Set uncommitted edits aside, update, then restore them |
| `--reset KEYS` | Restore the shipped default for these settings |
| `--dedupe` | Remove repeated settings, keeping the value in effect |

### Repairing a stale settings.conf

Because your values are never overwritten, a setting can go stale — it keeps
working while no longer matching what the code expects. `--check-settings`
warns about the cases that matter, and two options fix them without disturbing
the rest of the file. Both write a `.bak` first, and neither pulls code.

**`--reset KEYS`** restores the shipped default. Use it when a value is simply
wrong — for example a `PROMPT_TEMPLATE` still demanding a JSON object after the
code moved to arrays:

```bash
btcli update --reset PROMPT_TEMPLATE
btcli update --reset PROMPT_TEMPLATE,STRIP_TAGS
```

It restores the *default*, so it will discard a value you chose on purpose.

**`--dedupe`** removes repeated declarations of one setting. JSON keeps the last
of a repeated key and reports nothing, so a file can hold two values for one
setting and look perfectly fine — with no way to tell by reading which applies.
Dedupe keeps the one already in effect, so your behaviour does not change; the
file just stops disagreeing with itself:

```bash
btcli update --dedupe
```

Comments, ordering and formatting survive both, because they edit the raw text
rather than re-serialising the file.

Uncommitted edits to tracked files block an update, because a fast-forward
would clobber them — `--stash` is the way through. Untracked files never block
anything, and `settings.conf`, `.btcli.json` and `.btcli-cache.json` are all
gitignored, so your config and resume data survive every update.

## Troubleshooting

**Translations look shifted — line 3's text on line 1.** Fixed: every line now
carries an explicit ID and replies are validated against those IDs. If you have
output from an older version, re-translate it; `fix` cannot repair a shift.

**Arabic renders left-to-right, or letters are disconnected.** The player is
not applying bidi. Run `btcli fix -p PATH --apply rtl`. If the glyphs are
missing entirely, the player has no Arabic font — `--apply font` embeds one.

**`Invalid model response`.** The model returned an unexpected shape. Keep
`GEMINI_RESPONSE_SCHEMA` set to `true` so the reply shape is pinned.

**Constant rate limiting (429s).** Reduce `PARALLEL_CHUNKS` to 1, raise
`PARALLEL_COOLDOWN`, and order `MODEL_POOL` with the highest daily quota first.
Interruptions are safe — the cache means a re-run only sends what is missing.

**Some lines never translate.** Let the retry rounds finish, then choose to
pass them through or list them. Often it is one line the model refuses; raise
`PARTIAL_LINE_TOLERANCE` to get the file written with those lines in the source
language.

**Subtitles keep getting re-extracted.** The manifest lost track of them, or
they were deleted. This is expected with `--force`.

**`btcli update` refuses to run.** It is protecting local work. Read what it
says: rebase your commits, or pass `--stash` for uncommitted edits.

## Development

```bash
pip install -r requirements.txt
pip install pytest
python -m pytest
```

Tests build their subtitle fixtures programmatically, so no large or
third-party subtitle files live in the repo. CI runs the suite on Python 3.9
and 3.12, checks every module compiles and every help page renders, and builds
and installs the wheel.

```
btcli/
├── main.py         # CLI surface: argparse, help text, dispatch
├── config.py       # Settings loader and defaults
├── validate.py     # Settings validation (--check-settings)
├── interactive.py  # Guided mode, with back-navigation
├── discover.py     # File discovery
├── probe.py        # Probe flow
├── auto.py         # --auto track and style detection
├── classify.py     # Ask Gemini which track and styles are dialogue
├── extract.py      # ffmpeg extraction and track merging
├── manifest.py     # .btcli.json job records
├── cache.py        # .btcli-cache.json translation cache
├── srt_pre.py      # Parse SRT/ASS, clean text, keep tags
├── styles.py       # Style selection
├── blob.py         # Deduplicate and chunk
├── ai.py           # Gemini API, model ladder, pacing
├── translate.py    # Translate orchestrator
├── batch.py        # Incremental writing as responses arrive
├── retry.py        # Retry rounds for missing lines
├── sub_post.py     # Write output: RTL, styles, font embedding
├── fix.py          # Repair flow (no API calls)
├── preview.py      # --dry-run reporting
├── prune.py        # Reclaim cache and manifest state
├── update.py       # Self-update and settings merge
├── prompts.py      # Coloured terminal prompts
├── logger.py       # Levelled output
└── setup_key.py    # First-run API key setup
```
