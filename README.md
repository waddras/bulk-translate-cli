# bulk-translate-cli

Command-line subtitle translator. Extracts, translates, and outputs subtitle files using Gemini AI.

## Installation

```bash
git clone https://github.com/waddras/bulk-translate-cli
cd bulk-translate-cli
pip install -r requirements.txt
```

## Usage

### Probe (inspect files)

```bash
# Probe video files for subtitle tracks (sample mode — one per season dir)
btcli -probe -p "/media/anime/Haikyu!!"

# Probe all videos recursively
btcli -probe -p "/media/anime/Haikyu!!" -m recursive

# Probe subtitle files for styles
btcli -probe -p "/media/anime/Haikyu!!" -i sub -f ".en.ass"

# Probe with tags output
btcli -probe -p "/media/anime/Haikyu!!" -i sub -f ".en.ass" -o tags
```

### Translate

```bash
# Translate .en.ass files to Arabic (default)
btcli -translate -p "/media/anime/Haikyu!!" -i sub -f ".en.ass"

# Translate to French
btcli -translate -p "/media/anime/Haikyu!!" -i sub -f ".en.ass" -l "french"

# Extract track 0 from MKVs and translate to Arabic
btcli -translate -p "/media/anime/Haikyu!!" -t 0

# Custom suffix
btcli -translate -p "/media/anime/Haikyu!!" -i sub -f ".en.ass" -suffix ".arabic"

# Force SRT output
btcli -translate -p "/media/anime/Haikyu!!" -i sub -f ".en.ass" -o srt
```

## Configuration

All defaults are in `settings.conf` (JSON). The CLI looks for it in:
1. Current directory
2. `~/.config/btcli/settings.conf`
3. `/etc/btcli/settings.conf`

## Flags

| Flag | Probe | Translate | Description |
|---|---|---|---|
| `-p` / `--path` | required | required | Target path (file or directory) |
| `-m` / `--mode` | sample/recursive | N/A | Probe mode (default: sample) |
| `-i` / `--input` | vid/sub | vid/sub | Input file type (default: vid) |
| `-f` / `--filter` | optional | optional | Filter by filename pattern |
| `-o` / `--output` | tracks,styles,tags | srt | Output control |
| `-l` / `--lang` | N/A | target or source,target | Language (default: arabic) |
| `-t` / `--tracks` | N/A | track numbers | Tracks to extract (default: 0) |
| `-suffix` | N/A | auto from lang | Output filename suffix |
