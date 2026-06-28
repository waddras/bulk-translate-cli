"""Configuration loader: searches multiple paths for settings.conf (JSON).

Search order:
  1. ./settings.conf  (working directory)
  2. ~/.config/btcli/settings.conf
  3. /etc/btcli/settings.conf

First found wins; values merge over defaults.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# ── Search paths ──────────────────────────────────────────────────────────────
_INSTALL_DIR = Path("/opt/bulk-translate-cli")

_SEARCH_PATHS = [
    Path.cwd() / "settings.conf",
    _INSTALL_DIR / "settings.conf",
    Path.home() / ".config" / "btcli" / "settings.conf",
    Path("/etc/btcli/settings.conf"),
]

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    # API
    "GEMINI_API_KEY_FILE": "~/.btcli.env",
    "GEMINI_MODEL": "gemini-3.1-flash-lite",
    "MODEL_POOL": [
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3.5-flash",
    ],
    "GEMINI_MAX_OUTPUT_TOKENS": 0,

    # Translation
    "TRANSLATION_MODE": "chunked",
    "MAX_LINES_PER_CHUNK": 1000,
    "PARALLEL_CHUNKS": 1,
    "PARALLEL_COOLDOWN": 60,
    "RETRY_ATTEMPTS": 5,
    "RETRY_COOLDOWN": 10,
    "MAX_BLOB_LINES": 50000,
    "MAX_FAILED_CHUNKS": 5,

    # Language
    "SOURCE_LANGUAGE": "english",
    "TARGET_LANGUAGE": "arabic",
    "LANGUAGE_CODES": {
        "arabic": "ar",
        "french": "fr",
        "spanish": "es",
        "german": "de",
        "japanese": "ja",
        "english": "en",
        "portuguese": "pt",
        "italian": "it",
        "korean": "ko",
        "chinese": "zh",
    },

    # Output
    "FILE_CONFLICT": "overwrite",
    "EMBED_FONT": True,
    "FONT_NAME": "Noto Sans Arabic",
    "FONT_SIZE": 18,
    "FONT_OUTLINE": 1,
    "FONT_SHADOW": 0,
    "FONT_ALIGNMENT": 2,
    "FONT_MARGIN_L": 20,
    "FONT_MARGIN_R": 20,
    "FONT_MARGIN_V": 30,

    # Tags
    "PRESERVE_TAGS": "pos, an, move, fad, fade;",

    # Discovery
    "SOURCE_EXTENSIONS": [".srt", ".ass", ".ssa"],
    "MKV_EXTENSIONS": [".mkv", ".mp4", ".avi"],
    "SKIP_DIRS": ["Extras", "extras", "Featurettes", "featurettes", "Behind the Scenes"],

    # Prompt
    "PROMPT_TEMPLATE": (
        "You are a professional {source_language} to {target_language} subtitle translator.\n\n"
        "Context: These are subtitles from \"{show_name}\". The lines below form a "
        "continuous conversation in sequential order — use the full context to produce "
        "natural, flowing {target_language} dialogue.\n\n"
        "Rules:\n"
        "- Translate to natural, conversational {target_language}\n"
        "- These lines are a continuous scene — maintain consistency in tone, character voice, "
        "and references across all lines\n"
        "- Preserve humor, sarcasm, and emotional tone\n"
        "- Keep translations concise — must be readable as subtitles\n"
        "- NEVER censor, redact, or replace words with asterisks. Translate ALL content exactly "
        "as-is, including profanity, slurs, and adult language. This is professional subtitle "
        "work for mature audiences.\n\n"
        "Translate each value in the following JSON object.\n"
        "Return a valid JSON object with the EXACT same keys and ONLY {target_language} values.\n"
        "No extra keys, no explanation, no markdown.\n\n"
        "{json_blob}"
    ),
}


# ── Loader ────────────────────────────────────────────────────────────────────
_settings_file: Path | None = None


def _find_settings_file() -> Path | None:
    """Return first existing settings.conf from search paths."""
    for p in _SEARCH_PATHS:
        if p.exists():
            return p
    return None


def load_settings() -> dict:
    """Load settings.conf merged over defaults."""
    global _settings_file
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    _settings_file = _find_settings_file()
    if _settings_file:
        try:
            with open(_settings_file) as f:
                raw = f.read()
            # Strip // comments (not inside strings)
            import re
            cleaned = re.sub(r'(?m)^\s*//.*$', '', raw)
            cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)  # trailing commas
            user = json.loads(cleaned)
            # Deep merge for dicts (like LANGUAGE_CODES)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
        except Exception as e:
            print(f"[config] Warning: failed to load {_settings_file}: {e}")
    # Env var override for API key
    env_key = os.environ.get("GEMINI_API_KEY", "")
    if env_key:
        merged["GEMINI_API_KEY"] = env_key
    else:
        # Load from key file
        key_file = Path(os.path.expanduser(merged.get("GEMINI_API_KEY_FILE", "~/.btcli.env")))
        if key_file.exists():
            try:
                content = key_file.read_text().strip()
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY="):
                        merged["GEMINI_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
                    elif line and not line.startswith("#"):
                        # Bare key (no prefix)
                        merged["GEMINI_API_KEY"] = line
                        break
            except Exception:
                pass
        if "GEMINI_API_KEY" not in merged:
            merged["GEMINI_API_KEY"] = ""
    return merged


def save_settings(s: dict) -> None:
    """Persist current settings to the found (or default) settings.conf."""
    target = _settings_file or _SEARCH_PATHS[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w") as f:
        json.dump(s, f, indent=2)


# ── Live config ───────────────────────────────────────────────────────────────
cfg = load_settings()


def get(key: str, default=None):
    """Convenience accessor."""
    return cfg.get(key, default)


def get_lang_code(name: str) -> str:
    """Get 2-letter code for a language name."""
    codes = cfg.get("LANGUAGE_CODES", {})
    return codes.get(name.lower(), name[:2].lower())


def get_suffix_for_lang(lang: str) -> str:
    """Get output suffix from language (e.g. 'arabic' -> '.ar')."""
    return "." + get_lang_code(lang)
