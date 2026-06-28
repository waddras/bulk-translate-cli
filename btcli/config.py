#!/usr/bin/env python3
"""Configuration loader — reads settings.conf (JSON)."""
import json
from pathlib import Path

CONFIG_PATHS = [
    Path("settings.conf"),                          # current directory
    Path.home() / ".config/btcli/settings.conf",    # user config
    Path("/etc/btcli/settings.conf"),                # system config
]


def load_config() -> dict:
    """Load settings.conf from first found location."""
    for p in CONFIG_PATHS:
        if p.exists():
            with open(p) as f:
                cfg = json.load(f)
            print(f"[config] Loaded: {p}")
            return cfg
    print("[config] WARNING: No settings.conf found, using built-in defaults")
    return _defaults()


def _defaults() -> dict:
    return {
        "api_keys": [],
        "model_pool": [
            {"id": "gemini-2.5-flash", "rpd": 20, "rpm": 5, "priority": 1},
        ],
        "defaults": {
            "source_lang": "english",
            "target_lang": "arabic",
            "input_type": "vid",
            "track": 0,
            "probe_mode": "sample",
            "probe_output": "tracks,styles",
            "translate_output": "source",
            "max_lines_per_chunk": 1000,
            "parallel_chunks": 1,
            "parallel_cooldown": 60,
            "max_failed_chunks": 5,
            "retry_attempts": 5,
            "retry_cooldown": 10,
            "gemini_max_output_tokens": 0,
            "embed_font": True,
            "font_name": "IBM Plex Sans Arabic",
            "font_size": 40,
            "font_outline": 1,
            "font_shadow": 0,
            "font_alignment": 2,
            "font_margin_l": 20,
            "font_margin_r": 20,
            "font_margin_v": 30,
            "preserve_tags": "pos, an, move, fad, fade;",
            "prompt_template": "",
        },
        "skip_dirs": ["Extras", "Featurettes", "Specials", "extras"],
        "video_extensions": [".mkv", ".mp4", ".avi"],
        "subtitle_extensions": [".srt", ".ass", ".ssa"],
        "lang_codes": {
            "arabic": "ar", "french": "fr", "spanish": "es",
            "german": "de", "japanese": "ja", "english": "en",
        },
    }
