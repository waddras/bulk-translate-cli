"""Settings validation, run before a job starts.

settings.conf was previously trusted without inspection, so a mistake only
surfaced as a confusing runtime failure or, worse, as silently degraded
behaviour. The case that prompted this: GEMINI_MODEL repeated as MODEL_POOL[0]
combined with a low RETRY_ATTEMPTS meant an entire job only ever reached two of
seven configured models, and nothing said so.

Findings come at three levels:

* **error** — the job cannot run correctly. Always blocks.
* **warning** — it will run, but not as intended. Blocks under ``--strict``.
* **note** — worth knowing, but nothing to do. Never blocks, not even under
  ``--strict``, and does not stop the config being called good.

The third level exists because a warning that demands no action trains people
to ignore warnings that do. The case in point: RETRY_ATTEMPTS lower than the
number of distinct models used to warn on every single command, even though
``ai.effective_attempts()`` already raises it unconditionally, so there was
never anything for the user to fix — and ``--strict`` refused to run at all.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import cfg

# Types each setting must have. Numbers accept int only; bools are checked
# before ints because bool is a subclass of int in Python.
_EXPECTED_TYPES = {
    "GEMINI_MODEL": str,
    "MODEL_POOL": list,
    "GEMINI_MAX_OUTPUT_TOKENS": int,
    "GEMINI_RESPONSE_SCHEMA": bool,
    "TRANSLATION_MODE": str,
    "MAX_LINES_PER_CHUNK": int,
    "FILES_PER_BATCH": int,
    "PARALLEL_CHUNKS": int,
    "PARALLEL_COOLDOWN": int,
    "RETRY_ATTEMPTS": int,
    "RETRY_COOLDOWN": int,
    "MAX_BLOB_LINES": int,
    "MAX_FAILED_CHUNKS": int,
    "USE_TRANSLATION_CACHE": bool,
    "RESUME_PROMPT": bool,
    "PARTIAL_LINE_TOLERANCE": int,
    "SOURCE_LANGUAGE": str,
    "TARGET_LANGUAGE": str,
    "LANGUAGE_CODES": dict,
    "FILE_CONFLICT": str,
    "EMBED_FONT": bool,
    "FONT_NAME": str,
    "FONT_SIZE": int,
    "KEEP_TOP_STYLES": int,
    "SOURCE_EXTENSIONS": list,
    "MKV_EXTENSIONS": list,
    "SKIP_DIRS": list,
    "PROMPT_TEMPLATE": str,
}

_VALID_MODES = ("chunked", "multi_turn", "full_context")
_VALID_CONFLICT = ("overwrite", "rename")

# Settings that must be at least 1 to make any sense.
_POSITIVE = ("MAX_LINES_PER_CHUNK", "FILES_PER_BATCH", "PARALLEL_CHUNKS",
             "RETRY_ATTEMPTS", "MAX_BLOB_LINES", "MAX_FAILED_CHUNKS")

# Settings that may be zero but never negative.
_NON_NEGATIVE = ("PARALLEL_COOLDOWN", "RETRY_COOLDOWN", "PARTIAL_LINE_TOLERANCE",
                 "GEMINI_MAX_OUTPUT_TOKENS", "KEEP_TOP_STYLES", "FONT_SIZE")


def _type_name(expected) -> str:
    """A Python type as words, so messages read as advice not as a stack trace."""
    return {str: "text", int: "a whole number", bool: "true or false",
            list: "a list", dict: "an object"}.get(expected, str(expected))


def check_settings(settings: dict | None = None) -> tuple:
    """Inspect settings and return (errors, warnings, notes) as lists of strings.

    See the module docstring for what separates the three.
    """
    conf = cfg if settings is None else settings
    errors: list = []
    warnings: list = []
    notes: list = []

    # ── Types ────────────────────────────────────────────────────────────────
    for key, expected in _EXPECTED_TYPES.items():
        if key not in conf:
            continue
        value = conf[key]
        if expected is int and isinstance(value, bool):
            errors.append(f"{key} should be {_type_name(expected)}, not true/false")
        elif not isinstance(value, expected):
            errors.append(f"{key} should be {_type_name(expected)}, "
                          f"got {type(value).__name__}")

    # ── Ranges ───────────────────────────────────────────────────────────────
    for key in _POSITIVE:
        value = conf.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value < 1:
            errors.append(f"{key} must be at least 1 (currently {value})")

    for key in _NON_NEGATIVE:
        value = conf.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            errors.append(f"{key} cannot be negative (currently {value})")

    # ── Choices ──────────────────────────────────────────────────────────────
    mode = conf.get("TRANSLATION_MODE")
    if isinstance(mode, str) and mode not in _VALID_MODES:
        errors.append(f"TRANSLATION_MODE '{mode}' is not one of "
                      f"{', '.join(_VALID_MODES)}")

    conflict = conf.get("FILE_CONFLICT")
    if isinstance(conflict, str) and conflict not in _VALID_CONFLICT:
        errors.append(f"FILE_CONFLICT '{conflict}' is not one of "
                      f"{', '.join(_VALID_CONFLICT)}")

    # ── Models ───────────────────────────────────────────────────────────────
    primary = conf.get("GEMINI_MODEL")
    pool = conf.get("MODEL_POOL")

    if isinstance(primary, str) and not primary.strip():
        errors.append("GEMINI_MODEL is empty")

    if isinstance(pool, list):
        if any(not isinstance(m, str) or not m.strip() for m in pool):
            errors.append("MODEL_POOL must contain only non-empty model names")
        else:
            seen, duplicates = set(), []
            for model in pool:
                if model in seen and model not in duplicates:
                    duplicates.append(model)
                seen.add(model)
            if duplicates:
                warnings.append(
                    f"MODEL_POOL repeats {', '.join(duplicates)}; duplicates are "
                    f"ignored, so the retry ladder is shorter than it looks")

            distinct = len({m for m in pool} | ({primary} if isinstance(primary, str) else set()))
            attempts = conf.get("RETRY_ATTEMPTS")
            if isinstance(attempts, int) and not isinstance(attempts, bool) \
                    and 0 < attempts < distinct:
                # A note, not a warning: effective_attempts() has already
                # corrected this by the time anything runs, so there is nothing
                # to fix and no reason to block --strict.
                notes.append(
                    f"RETRY_ATTEMPTS is {attempts} but there are {distinct} distinct "
                    f"models; attempts are raised to {distinct} automatically so "
                    f"every model is tried. Nothing to change.")
        if not pool:
            warnings.append("MODEL_POOL is empty, so a failure has no fallback model")

    # ── Interactions ─────────────────────────────────────────────────────────
    chunk = conf.get("MAX_LINES_PER_CHUNK")
    blob = conf.get("MAX_BLOB_LINES")
    if all(isinstance(v, int) and not isinstance(v, bool) for v in (chunk, blob)):
        if chunk > blob:
            warnings.append(
                f"MAX_LINES_PER_CHUNK ({chunk}) exceeds MAX_BLOB_LINES ({blob}), "
                f"so a job large enough to chunk would be refused first")

    tolerance = conf.get("PARTIAL_LINE_TOLERANCE")
    if isinstance(chunk, int) and isinstance(tolerance, int) \
            and not isinstance(tolerance, bool) and tolerance > chunk:
        warnings.append(
            f"PARTIAL_LINE_TOLERANCE ({tolerance}) is larger than "
            f"MAX_LINES_PER_CHUNK ({chunk}); a whole failed chunk would still be "
            f"written with lines left in the source language")

    parallel = conf.get("PARALLEL_CHUNKS")
    cooldown = conf.get("PARALLEL_COOLDOWN")
    if isinstance(parallel, int) and not isinstance(parallel, bool) and parallel > 1 \
            and isinstance(cooldown, int) and cooldown == 0:
        warnings.append(
            f"PARALLEL_CHUNKS is {parallel} with no cooldown, which invites "
            f"rate limiting")

    # ── Languages ────────────────────────────────────────────────────────────
    codes = conf.get("LANGUAGE_CODES")
    target = conf.get("TARGET_LANGUAGE")
    source = conf.get("SOURCE_LANGUAGE")
    if isinstance(codes, dict):
        for label, language in (("TARGET_LANGUAGE", target), ("SOURCE_LANGUAGE", source)):
            if isinstance(language, str) and language.lower() not in codes:
                warnings.append(
                    f"{label} '{language}' has no entry in LANGUAGE_CODES; the "
                    f"suffix falls back to '.{language[:2].lower()}'")
    if isinstance(target, str) and isinstance(source, str) \
            and target.strip().lower() == source.strip().lower():
        warnings.append(f"SOURCE_LANGUAGE and TARGET_LANGUAGE are both "
                        f"'{target}', so nothing would change")

    # ── Prompt template ──────────────────────────────────────────────────────
    template = conf.get("PROMPT_TEMPLATE")
    if isinstance(template, str) and template:
        if "{json_blob}" not in template:
            warnings.append("PROMPT_TEMPLATE has no {json_blob} placeholder, so the "
                            "built-in prompt is used instead")
        lowered = template.lower()
        if "same keys" in lowered or "json object" in lowered:
            warnings.append(
                "PROMPT_TEMPLATE still asks for a JSON object with the same keys, "
                "but replies must be a JSON array; the built-in contract overrides "
                "it, so the model is being sent contradictory instructions. Fix "
                "with: btcli update --reset PROMPT_TEMPLATE")

    # ── Extensions ───────────────────────────────────────────────────────────
    for key in ("SOURCE_EXTENSIONS", "MKV_EXTENSIONS"):
        values = conf.get(key)
        if isinstance(values, list):
            bad = [v for v in values
                   if not isinstance(v, str) or not v.startswith(".")]
            if bad:
                errors.append(f"{key} entries must start with a dot: {bad}")

    return errors, warnings, notes


def find_duplicate_keys(raw: str) -> list:
    """Keys declared more than once in a settings file, in order of appearance.

    JSON keeps the last of a repeated key and reports nothing, so a file can
    hold two contradictory values for one setting and look fine. Reading it,
    there is no way to tell which one applies.
    """
    cleaned = re.sub(r'(?m)^\s*//.*$', '', raw)
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)

    duplicates: list = []

    def collect(pairs):
        """object_pairs_hook: sees every key before JSON collapses duplicates.

        Called for each object, so nested duplicates are caught too. Returns a
        plain dict, leaving normal parsing behaviour intact.
        """
        keys = [key for key, _ in pairs]
        for key in keys:
            if keys.count(key) > 1 and key not in duplicates:
                duplicates.append(key)
        return dict(pairs)

    try:
        json.loads(cleaned, object_pairs_hook=collect)
    except ValueError:
        return []          # unparseable is reported elsewhere
    return duplicates


def _duplicate_key_warnings(path) -> list:
    """Warn about any setting declared twice in the active settings file."""
    if path is None:
        return []
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []

    warnings = []
    for key in find_duplicate_keys(raw):
        warnings.append(
            f"{key} is declared more than once; JSON keeps only the last value, "
            f"so the earlier one is silently ignored. Delete the duplicate, or "
            f"reset it with: btcli update --reset {key}")
    return warnings


def report_settings(settings: dict | None = None, strict: bool = False,
                    announce_ok: bool = False) -> bool:
    """Log any problems. Returns False when the job should not start.

    Errors always block. Warnings block only when strict is set. Notes never
    block and never withhold the all-clear, because there is nothing to act on.

    With announce_ok, also says so when nothing blocks — but distinguishes a
    clean config from one that merely has nothing fatal. Claiming a config
    "looks good" directly beneath its own warnings reads as if the warnings did
    not count.
    """
    from .logger import log

    errors, warnings, notes = check_settings(settings)

    # Duplicates are invisible to check_settings, which only sees the parsed
    # dict — by then the repeated key has already collapsed to one value.
    if settings is None:
        from .config import _settings_file
        warnings = warnings + _duplicate_key_warnings(_settings_file)

    for message in errors:
        log.error(f"settings.conf: {message}")
    for message in warnings:
        log.warning(f"settings.conf: {message}")
    for message in notes:
        log.info(f"settings.conf: {message}")

    if errors:
        log.error("Fix the settings above, then run again.")
        return False
    if warnings and strict:
        log.error("Refusing to continue because of the warnings above (--strict).")
        return False

    if announce_ok:
        if warnings:
            log.warning(
                f"No blocking problems, but {len(warnings)} warning(s) above are "
                f"worth fixing. --strict treats them as errors.")
        else:
            log.success("settings.conf looks good.")
    return True
