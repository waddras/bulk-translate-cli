"""Configuration loader: reads settings.conf (JSON) from the repo root.

Provides `cfg` dict — all modules import this to read settings at call-time.
"""
import json
from pathlib import Path

# Locate settings.conf relative to this file (one level up = repo root)
_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.conf"

DEFAULT_SETTINGS = {
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "gemini-2.5-flash",
    "MAX_LINES_PER_CHUNK": 1000,
    "PARALLEL_CHUNKS": 1,
    "PARALLEL_COOLDOWN": 60,
    "GEMINI_MAX_OUTPUT_TOKENS": 0,
    "RETRY_ATTEMPTS": 5,
    "RETRY_COOLDOWN": 10,
    "MAX_BLOB_LINES": 50000,
    "MAX_FAILED_CHUNKS": 5,
    "TRANSLATION_MODE": "chunked",
    "FILE_CONFLICT": "overwrite",
    "EMBED_FONT": False,
    "PRESERVE_TAGS": "pos, an, move, fad, fade;",
    "FONT_NAME": "Amiri",
    "FONT_SIZE": 40,
    "FONT_OUTLINE": 1,
    "FONT_SHADOW": 0,
    "FONT_ALIGNMENT": 2,
    "FONT_MARGIN_L": 20,
    "FONT_MARGIN_R": 20,
    "FONT_MARGIN_V": 30,
    "PROMPT_TEMPLATE": (
        "You are a professional English to Arabic subtitle translator.\n\n"
        "Context: These are subtitles from \"{show_name}\". The lines below form a "
        "continuous conversation in sequential order — use the full context to produce "
        "natural, flowing Arabic dialogue.\n\n"
        "Rules:\n"
        "- Translate to Modern Standard Arabic (MSA), but keep dialogue natural and conversational\n"
        "- These lines are a continuous scene — maintain consistency in tone, character voice, "
        "and references across all lines\n"
        "- Preserve humor, sarcasm, and emotional tone\n"
        "- Keep translations concise — must be readable as subtitles\n\n"
        "Translate each value in the following JSON object.\n"
        "Return a valid JSON object with the EXACT same keys and ONLY Arabic values.\n"
        "No extra keys, no explanation, no markdown.\n\n"
        "{json_blob}"
    ),
    "SOURCE_EXTENSIONS": [".srt", ".ass"],
    "MKV_EXTENSIONS": [".mkv", ".mp4", ".avi"],
}


def load_settings() -> dict:
    """Load settings.conf merged over defaults."""
    merged = dict(DEFAULT_SETTINGS)
    try:
        if _SETTINGS_PATH.exists():
            with open(_SETTINGS_PATH) as f:
                merged.update(json.load(f))
    except Exception as e:
        print(f"[config] Warning: failed to load {_SETTINGS_PATH}: {e}")
    return merged


def save_settings(s: dict) -> None:
    """Persist current settings to settings.conf."""
    with open(_SETTINGS_PATH, "w") as f:
        json.dump(s, f, indent=2)


# Live config — import this from other modules
cfg = load_settings()


def get(key: str, default=None):
    """Convenience accessor."""
    return cfg.get(key, default)
