# bulk-translate-cli

CLI tool for bulk subtitle translation using the Gemini API.

Command-line counterpart to [bulk-translate](https://github.com/waddras/bulk-translate) (web UI version).

## Features

- **Probe** video/subtitle files for tracks, styles, and tags
- **Translate** SRT/ASS subtitles via Gemini (chunked, multi-turn, or full-context mode)
- **Multi-language** — any source/target pair (English, Arabic, French, Japanese, etc.)
- **Multi-track** — extract and combine multiple subtitle tracks from MKVs
- **Deduplication** — identical lines translated once, fanned back out
- **Model cycling** — rotates through MODEL_POOL on retries
- **Font embedding** — subsetted Arabic font in ASS output
- **RTL wrapping** — proper bidi marks (RLI+PDI) per line

## Installation

```bash
pip install -r requirements.txt
```

Requires `ffmpeg` and `ffprobe` on PATH for video operations.

## Configuration

Settings are loaded from the first found:
1. `./settings.conf` (working directory)
2. `~/.config/btcli/settings.conf`
3. `/etc/btcli/settings.conf`

Set your API key:
```bash
export GEMINI_API_KEY="your-key"
```
Or add it to `settings.conf`:
```json
{ "GEMINI_API_KEY": "your-key" }
```

## Usage

### Probe

```bash
# Probe video files for subtitle tracks (sample mode — one per subdir)
btcli probe -p "/home/show01" -i vid -m sample

# Probe all video files recursively
btcli probe -p "/home/show01" -i vid -m recursive

# Probe subtitle files for styles
btcli probe -p "/home/show01" -i sub

# Probe with tag scanning
btcli probe -p "/home/show01" -i sub -o tags

# Filter to specific files
btcli probe -p "/home/show01" -i sub -f ".en.ass"
```

### Translate

```bash
# Translate subtitle files (default: english → arabic)
btcli translate -p "/home/show01"

# From video files (extract track 0 + translate)
btcli translate -p "/home/show01" -i vid -t 0

# Combine multiple tracks
btcli translate -p "/home/show01" -i vid -t 0,1,2,3

# Different target language
btcli translate -p "/home/show01" -l french

# Explicit source,target pair
btcli translate -p "/home/show01" -l "japanese,english"

# Filter files
btcli translate -p "/home/show01" -i sub -f ".en.ass"

# Custom suffix
btcli translate -p "/home/show01" -suffix ".ar"

# Force SRT output
btcli translate -p "/home/show01" -o srt

# Override show name
btcli translate -p "/home/show01" --show-name "Breaking Bad"
```

## Translation Pipeline

1. **Discover** — find files (recursive, with skip_dirs and filter)
2. **Extract** — if input is video, extract/combine tracks via ffmpeg
3. **Parse** — load SRT/ASS, clean text, preserve positioning tags
4. **Blob** — build deduplicated blob with `FFLLLL` keys
5. **Chunk** — split into chunks <= MAX_LINES_PER_CHUNK
6. **Translate** — send to Gemini API using configured mode
7. **Retry** — re-send missing lines with context, cycling models
8. **Reassemble** — write output files with RTL marks + font style

## Translation Modes

| Mode | Description |
|------|-------------|
| `chunked` | Independent chunks, parallel batches with cooldown (default) |
| `multi_turn` | Full blob as context, chunks as conversation turns |
| `full_context` | Full blob sent every request, only specific keys translated |

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `GEMINI_API_KEY` | `""` | API key (or set env var) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Primary model |
| `MODEL_POOL` | `[2.5-flash, 2.0-flash, 1.5-flash]` | Models to cycle on retry |
| `TRANSLATION_MODE` | `chunked` | Mode: chunked, multi_turn, full_context |
| `MAX_LINES_PER_CHUNK` | `1000` | Max lines per API call |
| `PARALLEL_CHUNKS` | `1` | Simultaneous chunks per batch |
| `PARALLEL_COOLDOWN` | `60` | Seconds between API calls |
| `RETRY_ATTEMPTS` | `5` | Retries per chunk |
| `MAX_FAILED_CHUNKS` | `5` | Max retry rounds for missing lines |
| `SOURCE_LANGUAGE` | `english` | Default source language |
| `TARGET_LANGUAGE` | `arabic` | Default target language |
| `FILE_CONFLICT` | `overwrite` | `overwrite` or `rename` |
| `EMBED_FONT` | `false` | Embed subsetted font in ASS |
| `PRESERVE_TAGS` | `pos, an, move, fad, fade;` | ASS tags to preserve |
| `SKIP_DIRS` | `[Extras, ...]` | Directories to skip |

## Project Structure

```
btcli/
├── __init__.py      # Version
├── __main__.py      # python -m btcli entry
├── main.py          # CLI (argparse, two subcommands)
├── config.py        # Settings loader (multi-path search)
├── discover.py      # File discovery (sample/recursive, skip_dirs)
├── extract.py       # ffmpeg extraction + multi-track combining
├── srt_pre.py       # Parse SRT/ASS, clean text, preserve tags
├── blob.py          # Build blob, dedup, split chunks
├── ai.py            # Gemini API (3 modes + model cycling)
├── sub_post.py      # Reassemble output (ASS/SRT, font embed, RTL)
├── probe.py         # Probe flow (vid/sub, tracks/styles/tags)
└── translate.py     # Translate orchestrator (ties pipeline together)
```
