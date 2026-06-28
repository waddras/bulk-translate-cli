# bulk-translate-cli

CLI tool for bulk subtitle translation (English → Arabic) using the Gemini API.

This is the command-line counterpart to [bulk-translate](https://github.com/waddras/bulk-translate) (web UI version).

## Features

- **Probe** MKV files for subtitle tracks
- **Extract** subtitle tracks from MKV files (via ffmpeg)
- **Translate** SRT/ASS subtitle files to Arabic using Gemini
- **Deduplication** — identical lines translated once, fanned back out
- **Chunked translation** with automatic retry for missing lines
- **ASS style filtering** — translate only dialogue styles
- **RTL wrapping** — proper bidi marks for Arabic output

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Edit `settings.conf` (JSON format) in the repo root:

```json
{
  "GEMINI_API_KEY": "your-api-key-here",
  "GEMINI_MODEL": "gemini-2.5-flash",
  "MAX_LINES_PER_CHUNK": 1000,
  ...
}
```

Or set the `GEMINI_API_KEY` environment variable:

```bash
export GEMINI_API_KEY="your-key"
```

## Usage

```bash
# List subtitle files in a directory
python -m btcli list /path/to/subs

# List MKV files
python -m btcli list /path/to/videos -m extract

# Probe MKV files for subtitle tracks
python -m btcli probe /path/to/videos

# Extract subtitle track 0 from MKVs
python -m btcli extract /path/to/videos --track 0 --suffix ".en"

# Extract and convert ASS to SRT
python -m btcli extract /path/to/videos --track 0 --suffix ".en" --to-srt

# Detect ASS styles
python -m btcli styles /path/to/subs

# Translate all subtitle files in a directory
python -m btcli translate /path/to/subs

# Translate specific files
python -m btcli translate file1.srt file2.ass

# Translate only specific ASS styles
python -m btcli translate /path/to/subs --styles Default Dialogue

# Override show name (used in translation prompt)
python -m btcli translate /path/to/subs --show-name "My Show"

# Recursive search
python -m btcli translate /path/to/subs -r
```

## Pipeline

The translation flow:

1. **Parse** — Load SRT/ASS files, clean text, preserve positioning tags
2. **Blob** — Build deduplicated translation blob (identical lines sent once)
3. **Chunk** — Split into chunks ≤ MAX_LINES_PER_CHUNK
4. **Translate** — Send to Gemini API with retry logic
5. **Retry** — Re-send missing lines with surrounding context
6. **Reassemble** — Write `.ar.srt` or `.ar.ass` output files with RTL marks

## Output

- Input: `Show - S01E01.en.srt` → Output: `Show - S01E01.ar.srt`
- Input: `Show - S01E01.ass` → Output: `Show - S01E01.ar.ass`

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `GEMINI_API_KEY` | `""` | Gemini API key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model to use |
| `MAX_LINES_PER_CHUNK` | `1000` | Max lines per API call |
| `PARALLEL_CHUNKS` | `1` | Chunks sent simultaneously |
| `PARALLEL_COOLDOWN` | `60` | Seconds between API calls |
| `RETRY_ATTEMPTS` | `5` | Retries per chunk |
| `RETRY_COOLDOWN` | `10` | Seconds between retries |
| `MAX_FAILED_CHUNKS` | `5` | Max retry rounds for missing lines |
| `FILE_CONFLICT` | `overwrite` | `overwrite` or `rename` |
| `PRESERVE_TAGS` | `pos, an, move, fad, fade;` | ASS tags to preserve |
| `PROMPT_TEMPLATE` | *(see settings.conf)* | Gemini prompt template |
