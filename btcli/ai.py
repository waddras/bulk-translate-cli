"""Talking to Gemini, and refusing to believe it without proof.

TRANSLATION MODES (``TRANSLATION_MODE``)
    chunked       default. Independent chunks, sent in batches of
                  PARALLEL_CHUNKS with a cooldown between batches. Cheapest,
                  and a failure only affects its own chunk.
    multi_turn    one conversation; each chunk is a turn, so earlier chunks
                  stay in context. Better consistency, more tokens.
    full_context  the whole blob accompanies every request, with only specific
                  keys asked for. Best consistency, most expensive by far.

THE VALIDATION THAT MATTERS
    This module exists in its current shape because of a real corruption bug:
    the model returned translations shifted by a few positions, under keys that
    all looked correct, and everything downstream believed it. Subtitles were
    silently attributed to the wrong cues.

    The defence is doubled identity. Every payload item carries an inline
    ``<BTCLI_ID:NNNNNN>`` token *inside its text* as well as an ``id`` field, and
    ``_normalize_result`` accepts a translation only when the two agree, the ID
    is one this chunk actually asked for, and it has not already been seen.
    Anything else is dropped and re-requested individually.

    A shifted response therefore fails to validate rather than being accepted,
    which is the whole point. Do not relax these checks to reduce retries — that
    trades a visible cost for an invisible one.

    ``_output_contract`` states the required reply shape and says explicitly that
    it overrides earlier wording, because it is appended after the user's
    PROMPT_TEMPLATE, which may say something older and contradictory.

THE MODEL LADDER
    ``GEMINI_MODEL`` and ``MODEL_POOL`` merge into one de-duplicated ladder. A
    failure moves to the next model immediately — a fresh model has its own
    quota, so waiting on the one that just refused achieves nothing. Only once
    the ladder has been walked does ``backoff_before_retry`` actually wait.
    ``effective_attempts`` guarantees the ladder is never cut short by a low
    RETRY_ATTEMPTS.

PACING
    One helper, ``pace_requests``, enforces PARALLEL_COOLDOWN for every mode. It
    counts time already spent, so a call that took 40s of a 60s cooldown waits
    20s rather than another 60. Uses a monotonic clock so a system clock change
    cannot cause an hours-long wait.

LINE BREAKS
    Multi-line cues are protected by a ``<BTCLI_LB>`` sentinel rather than a raw
    newline, which models reliably mangle. ``_restore_line_breaks`` puts them
    back and, if the count changed anyway, rebuilds it from the source so two
    cues can never merge into one.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from .config import cfg
from .logger import log

# ── Constants ─────────────────────────────────────────────────────────────────
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_last_api_call = 0.0
LINE_BREAK_SENTINEL = "<BTCLI_LB>"
INLINE_ID_RE = re.compile(r"^\s*<BTCLI_ID:([^>]+)>\s*")


def _wire_text(tag: str, text: str) -> str:
    """Attach an inline ID and protect source line breaks for the model."""
    protected = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", LINE_BREAK_SENTINEL)
    return f"<BTCLI_ID:{tag}> {protected}"


def _wire_items(payload: dict) -> list:
    """Build the wire payload as a JSON array of {id, text} items.

    An array is used because models reliably mirror this shape, and each item
    carries its own id so a translation can never be attributed to another cue.
    """
    return [
        {"id": tag, "text": _wire_text(tag, text)}
        for tag, text in payload.items()
    ]


# Wrapper keys a model may wrap the array in (tolerated on input).
_WRAPPER_KEYS = ("translations", "items", "lines", "results", "data", "output")


def _response_schema() -> dict:
    """Schema pinning the response to an array of {id, text} objects."""
    return {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "id": {"type": "STRING"},
                "text": {"type": "STRING"},
            },
            "required": ["id", "text"],
        },
    }


def _output_contract(target_lang: str) -> str:
    """The reply format btcli requires, appended after the user's prompt template.

    Placed last and explicitly declared as overriding, because a user's
    PROMPT_TEMPLATE may still carry older wording asking for a different shape.
    Whatever the template says, this is what ``_normalize_result`` enforces, so
    the template must not specify a shape of its own — ``validate.py`` warns when
    it does.
    """
    return (
        "BTCLI OUTPUT CONTRACT (this overrides any earlier output-format wording):\n"
        "- Return a JSON ARRAY. Each element is an object with exactly two fields: id and text.\n"
        "- Return one element per source item, in the same order, with the id copied verbatim.\n"
        "- Keep the <BTCLI_ID:...> token at the start of text, matching that element's id.\n"
        f"- Translate only the text after the inline ID token to {target_lang}.\n"
        f"- Preserve every {LINE_BREAK_SENTINEL} token exactly; never remove, translate, or move it.\n"
        "- Never merge, split, skip, renumber, or reorder source items.\n"
        "- Return JSON only, with no markdown, keys, or explanation around the array.\n"
    )


def _rebalance_line_breaks(text: str, source: str) -> str:
    """Restore the source line count if the model dropped protected breaks."""
    expected_lines = source.split("\n")
    line_count = len(expected_lines)
    flat = " ".join(text.replace(LINE_BREAK_SENTINEL, " ").replace(r"\N", " ").split())
    if line_count <= 1 or not flat:
        return flat

    # Dual-speaker cues are best split at their dialogue markers.
    if all(re.match(r"^\s*(?:--|[-–—])", line) for line in expected_lines):
        parts = re.split(r"\s+(?=(?:--|[-–—])\s*)", flat)
        if len(parts) == line_count:
            return "\n".join(part.strip() for part in parts)

    # Cosmetic wrapping: preserve the number of source lines, but rebalance at
    # target-language word boundaries according to source line proportions.
    words = flat.split()
    if len(words) >= line_count:
        weights = [max(1, len(line)) for line in expected_lines]
        total_weight = sum(weights)
        cuts = []
        cumulative = 0
        previous = 0
        for weight in weights[:-1]:
            cumulative += weight
            cut = round(len(words) * cumulative / total_weight)
            cut = max(previous + 1, min(cut, len(words) - (line_count - len(cuts) - 1)))
            cuts.append(cut)
            previous = cut
        result = []
        start = 0
        for cut in cuts + [len(words)]:
            result.append(" ".join(words[start:cut]))
            start = cut
        return "\n".join(result)

    # Very short translations: character-level fallback.
    size = max(1, len(flat) // line_count)
    parts = [flat[i * size:(i + 1) * size].strip() for i in range(line_count - 1)]
    parts.append(flat[(line_count - 1) * size:].strip())
    return "\n".join(parts)


def _restore_line_breaks(text: str, source: str) -> str:
    """Turn the protected sentinels back into real newlines.

    Accepts a literal ``\\N`` too, since models sometimes helpfully "correct" the
    sentinel into ASS syntax.

    If the line count still does not match the source, the text is rebalanced
    locally rather than accepted as-is: a dropped break would merge two subtitle
    lines into one, which is visible and wrong on screen.
    """
    restored = text.replace(LINE_BREAK_SENTINEL, "\n").replace(r"\N", "\n")
    expected = source.count("\n")
    if restored.count("\n") == expected:
        return restored
    log.detail("    Model changed a line-break marker; restored source line count locally")
    return _rebalance_line_breaks(restored, source)


def _iter_response_items(result):
    """Yield (outer_key, item) pairs from any supported response shape.

    Supported: a bare array, an array wrapped in a single key, or a keyed object.
    ``outer_key`` is None for array shapes, where no outer key exists to compare.
    """
    if isinstance(result, list):
        for item in result:
            yield None, item
        return

    if isinstance(result, dict):
        for wrapper in _WRAPPER_KEYS:
            inner = result.get(wrapper)
            if isinstance(inner, list):
                for item in inner:
                    yield None, item
                return
        for key, value in result.items():
            yield str(key), value


def _describe(result) -> str:
    """Short description of an unexpected response, for diagnostics."""
    preview = repr(result)
    if len(preview) > 400:
        preview = preview[:400] + "..."
    return f"type={type(result).__name__} preview={preview}"


def _normalize_result(result, expected: dict) -> dict:
    """Validate returned IDs and return only safely-attributable translations.

    Identity is carried by each item's own ``id`` plus the inline BTCLI_ID token
    inside the text, so a shifted or merged translation cannot be silently
    attributed to the wrong cue. Rejected keys are left for targeted retry.
    """
    if not isinstance(result, (list, dict)):
        log.detail(f"    Invalid model response: JSON root is not an array or object ({_describe(result)})")
        return {}

    normalized = {}
    seen_ids = set()
    invalid_ids = set()
    items_seen = 0

    for outer_key, value in _iter_response_items(result):
        items_seen += 1
        label = outer_key if outer_key is not None else f"item {items_seen}"
        if not isinstance(value, dict):
            log.detail(f"    Rejecting {label}: element does not contain id/text fields")
            continue
        inner_id = str(value.get("id", ""))
        text = value.get("text")
        if not isinstance(text, str):
            log.detail(f"    Rejecting {label}: translated text is not a string")
            continue
        match = INLINE_ID_RE.match(text)
        inline_id = match.group(1) if match else ""
        if not inner_id or not inline_id or inner_id != inline_id:
            log.detail(f"    Rejecting {label}: id field and inline ID token do not match")
            continue
        if inner_id not in expected:
            log.detail(f"    Ignoring unexpected inline ID: {inner_id}")
            continue
        if inner_id in seen_ids:
            log.detail(f"    Rejecting duplicate inline ID: {inner_id}")
            invalid_ids.add(inner_id)
            continue
        seen_ids.add(inner_id)
        # Keyed-object shape only: the outer key must agree with the inline ID.
        if outer_key is not None and outer_key != inner_id:
            log.detail(f"    Rejecting {label}: outer key does not match inline ID {inner_id}")
            invalid_ids.add(inner_id)
            continue
        translated_text = INLINE_ID_RE.sub("", text, count=1)
        normalized[inner_id] = _restore_line_breaks(translated_text, expected[inner_id])

    for invalid_id in invalid_ids:
        normalized.pop(invalid_id, None)

    if not normalized:
        log.detail(f"    No usable translations in response ({items_seen} element(s) parsed)")
        if items_seen == 0:
            log.detail(f"    Response shape: {_describe(result)}")

    missing = set(expected) - set(normalized)
    if missing:
        log.detail(f"    ID validation left {len(missing)} key(s) for targeted retry")
    return normalized


def _notify_progress(callback, translated: dict) -> None:
    """Hand results to the caller as they arrive, never letting it break the run.

    The callback writes to the cache and generates finished files. If it raises,
    the failure is logged and translation continues: losing an incremental write
    is recoverable, but abandoning a part-finished API run wastes real quota.
    """
    if callback:
        try:
            callback(translated)
        except Exception as exc:
            log.detail(f"    Output progress callback failed: {exc}")


# ── Cooldown ──────────────────────────────────────────────────────────────────

def reset_pacing() -> None:
    """Forget when the last request group ran, so the next one is not delayed."""
    global _last_api_call
    _last_api_call = 0.0


async def pace_requests() -> None:
    """Wait out PARALLEL_COOLDOWN before the next group of requests.

    The time already spent since the previous group counts towards the cooldown,
    so a call that itself took 40s of a 60s cooldown waits 20s rather than a
    further 60s. Uses a monotonic clock, so a system time change cannot cause a
    wait of hours or skip the cooldown entirely.

    Called once per batch in chunked mode, which sends PARALLEL_CHUNKS requests
    together, and once per request in the modes that send one at a time.
    """
    global _last_api_call
    cooldown = max(0, cfg.get("PARALLEL_COOLDOWN", 60) or 0)
    if _last_api_call and cooldown:
        remaining = cooldown - (time.monotonic() - _last_api_call)
        if remaining > 0:
            log.cooldown(remaining)
            await asyncio.sleep(remaining)
    _last_api_call = time.monotonic()


async def backoff_before_retry(attempt: int, attempts: int) -> bool:
    """Wait before retrying, unless there is no point waiting. Returns True if it waited.

    Skipped when this was the last attempt, and when the next attempt moves to a
    different model — a fresh model has its own quota, so waiting on the one that
    just failed achieves nothing.

    The wait grows with the attempt number, which is what RETRY_COOLDOWN
    documents. Every retry path uses this, so a 429 and a parse failure back off
    the same way.
    """
    if attempt >= attempts or _switching_model(attempt):
        return False
    cooldown = max(0, cfg.get("RETRY_COOLDOWN", 10) or 0)
    await asyncio.sleep(cooldown * attempt)
    return True


# ── Prompt Building ───────────────────────────────────────────────────────────

def _build_prompt(chunk: dict, show_name: str = "",
                  source_lang: str = "english", target_lang: str = "arabic") -> str:
    """Build translation prompt with an inline-ID array payload."""
    template = cfg.get("PROMPT_TEMPLATE", "")
    wire_json = json.dumps(_wire_items(chunk), ensure_ascii=False)
    if template and "{json_blob}" in template:
        name = show_name or "Unknown"
        base = (
            template
            .replace("{show_name}", name)
            .replace("{source_language}", source_lang)
            .replace("{target_language}", target_lang)
            .replace("{json_blob}", "")
        )
    else:
        base = (
            f"You are a professional {source_lang} to {target_lang} subtitle translator.\n"
            f"Translate every source item to {target_lang}.\n"
        )
    return f"{base.rstrip()}\n\n{_output_contract(target_lang)}\nPayload:\n{wire_json}"


def _build_full_context_prompt(translate_keys: list, full_blob: dict,
                               show_name: str = "", source_lang: str = "english",
                               target_lang: str = "arabic") -> str:
    """Build prompt for full_context mode."""
    return (
        f"You are a professional {source_lang} to {target_lang} subtitle translator.\n"
        f"Context: Subtitles from \"{show_name or 'Unknown'}\".\n\n"
        f"Below is the FULL dialogue. Translate ONLY the keys listed below.\n"
        f"Keys to translate: {json.dumps(translate_keys)}\n\n"
        f"{_output_contract(target_lang)}\n"
        f"Return ONLY the requested keys.\n\n"
        f"Full dialogue:\n"
        f"{json.dumps(_wire_items(full_blob), ensure_ascii=False)}"
    )


def _build_retry_prompt(translate_keys: list, context: dict,
                        show_name: str = "", source_lang: str = "english",
                        target_lang: str = "arabic") -> str:
    """Build prompt for retry with surrounding context."""
    return (
        f"You are a professional {source_lang} to {target_lang} subtitle translator.\n"
        f"Context: Subtitles from \"{show_name or 'Unknown'}\".\n\n"
        f"Below is a section of dialogue. Translate ONLY the keys listed below.\n"
        f"Keys to translate: {json.dumps(translate_keys)}\n\n"
        f"{_output_contract(target_lang)}\n"
        f"Return ONLY the requested keys.\n\n"
        f"Dialogue section:\n"
        f"{json.dumps(_wire_items(context), ensure_ascii=False)}"
    )


def _generation_config() -> dict:
    """Build Gemini generation config.

    When GEMINI_RESPONSE_SCHEMA is enabled the response is structurally pinned
    to an array of {id, text} objects, so the model cannot pick another shape.
    """
    gen = {"temperature": 0.1, "responseMimeType": "application/json"}
    if cfg.get("GEMINI_RESPONSE_SCHEMA", True):
        gen["responseSchema"] = _response_schema()
    max_out = cfg.get("GEMINI_MAX_OUTPUT_TOKENS", 0)
    if max_out and max_out > 0:
        gen["maxOutputTokens"] = max_out
    return gen



# ── Model Pool ────────────────────────────────────────────────────────────────

def model_ladder() -> list:
    """Every distinct model to try, in order, primary first.

    GEMINI_MODEL is merged with MODEL_POOL and duplicates are dropped, so a pool
    that repeats the primary (the shipped default does) cannot waste a retry on
    the model that just failed.
    """
    primary = cfg.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    pool = cfg.get("MODEL_POOL", []) or []
    ladder = []
    for model in [primary] + list(pool):
        if model and model not in ladder:
            ladder.append(model)
    return ladder or [primary]


def effective_attempts() -> int:
    """Attempts per chunk: never fewer than the number of distinct models.

    This guarantees every model in the ladder is tried before a chunk is given
    up on, whatever RETRY_ATTEMPTS is set to.
    """
    return max(int(cfg.get("RETRY_ATTEMPTS", 5) or 1), len(model_ladder()))


def _get_model_for_attempt(attempt: int) -> str:
    """Model for a 1-based attempt number, walking the ladder then wrapping."""
    ladder = model_ladder()
    return ladder[(attempt - 1) % len(ladder)]


def _switching_model(attempt: int) -> bool:
    """True when the next attempt uses a different model than this one.

    Backing off is only useful when the same model is about to be retried;
    moving to a different model should happen immediately.
    """
    ladder = model_ladder()
    return len(ladder) > 1 and attempt < len(ladder)


# ── Core API Call ─────────────────────────────────────────────────────────────

async def _call_gemini(client: httpx.AsyncClient, prompt: str, api_key: str,
                       model: str | None = None, attempt: int = 1) -> dict | None:
    """Make a single Gemini API call. Returns parsed JSON response or None."""
    import json_repair

    if model is None:
        model = _get_model_for_attempt(attempt)

    url = f"{GEMINI_BASE}/{model}:generateContent"
    gen_cfg = _generation_config()

    try:
        response = await client.post(
            url,
            headers={"x-goog-api-key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": gen_cfg,
            },
            timeout=300.0,
        )

        if response.status_code == 429:
            log.detail(f"    Rate limited (429) - model: {model}")
            return None

        response.raise_for_status()
        raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        result = json_repair.loads(raw)
        return result

    except httpx.HTTPStatusError as e:
        log.detail(f"    HTTP {e.response.status_code} - model: {model}")
        return None
    except Exception as e:
        log.detail(f"    ERROR: {e} - model: {model}")
        return None



# ── Chunked Mode ──────────────────────────────────────────────────────────────

async def translate_chunked(client: httpx.AsyncClient, chunks: list, api_key: str,
                            show_name: str = "", source_lang: str = "english",
                            target_lang: str = "arabic", progress_callback=None) -> dict:
    """Translate using chunked mode: independent chunks with retry."""
    from .blob import estimate_output_tokens

    translated = {}
    parallel = max(1, cfg.get("PARALLEL_CHUNKS", 1))
    total = len(chunks)
    failures: list = []

    async def _translate_one(chunk, chunk_num):
        """Translate a single chunk, trying every model before giving up."""
        est = estimate_output_tokens(chunk)
        ladder = model_ladder()
        attempts = effective_attempts()
        log.chunk_status(chunk_num, total, len(chunk), est,
                         f"{ladder[0]} (+{len(ladder) - 1} fallback)"
                         if len(ladder) > 1 else ladder[0])

        prompt = _build_prompt(chunk, show_name, source_lang, target_lang)

        for attempt in range(1, attempts + 1):
            model = _get_model_for_attempt(attempt)
            log.detail(f"    Attempt {attempt}/{attempts} - model: {model}")
            result = await _call_gemini(client, prompt, api_key, model=model, attempt=attempt)
            if result:
                normalized = _normalize_result(result, chunk)
                if normalized:
                    translated.update(normalized)
                    _notify_progress(progress_callback, translated)
                    log.chunk_success(chunk_num, len(normalized))
                    log.advance_progress()
                    return normalized
                log.attempt(attempt, attempts, f"{model}: response failed ID validation")
            else:
                log.attempt(attempt, attempts, f"{model}: failed")
            await backoff_before_retry(attempt, attempts)

        # A failed chunk is deliberately NOT counted as progress: completing the
        # bar stops its elapsed clock, which reads as a frozen display while
        # retries are still running.
        failures.append(chunk_num)
        log.chunk_fail(chunk_num, f"after {attempts} attempts across "
                                  f"{len(ladder)} model(s)")
        log.update_progress(description=f"Translating ({len(failures)} failed)")
        return None

    # Send chunks in parallel batches, pacing between batches rather than
    # between individual calls, so PARALLEL_CHUNKS requests still go out together.
    for batch_start in range(0, total, parallel):
        await pace_requests()

        batch = chunks[batch_start:batch_start + parallel]
        tasks = [
            _translate_one(chunk, batch_start + i + 1)
            for i, chunk in enumerate(batch)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                log.detail(f"    Batch error: {result}")
            elif result:
                translated.update(result)

    return translated



# ── Multi-Turn Mode ───────────────────────────────────────────────────────────

async def translate_multi_turn(client: httpx.AsyncClient, chunks: list,
                               full_payload: dict, api_key: str,
                               show_name: str = "", source_lang: str = "english",
                               target_lang: str = "arabic", progress_callback=None) -> dict:
    """Translate using multi_turn mode: full blob as context, chunks as turns."""
    from .blob import estimate_output_tokens
    import json_repair

    translated = {}
    gen_cfg = _generation_config()
    ladder = model_ladder()
    attempts = effective_attempts()
    failures: list = []

    context_text = (
        f"You are a professional {source_lang} to {target_lang} subtitle translator.\n"
        f"Context: Subtitles from \"{show_name or 'Unknown'}\".\n\n"
        f"Here is the full dialogue for reference:\n"
        f"{json.dumps(_wire_items(full_payload), ensure_ascii=False)}\n\n"
        f"I will send you subsets of keys to translate.\n"
        f"{_output_contract(target_lang)}"
    )

    contents = [{"role": "user", "parts": [{"text": context_text}]}]

    for chunk_num, chunk in enumerate(chunks, 1):
        est = estimate_output_tokens(chunk)
        log.chunk_status(chunk_num, len(chunks), len(chunk), est,
                         f"{ladder[0]} (+{len(ladder) - 1} fallback)"
                         if len(ladder) > 1 else ladder[0])

        keys_to_translate = list(chunk.keys())
        turn_text = (
            f"Translate these keys:\n"
            f"{json.dumps(keys_to_translate)}\n\n"
            f"Values:\n"
            f"{json.dumps(_wire_items(chunk), ensure_ascii=False)}"
        )

        current_contents = contents + [{"role": "user", "parts": [{"text": turn_text}]}]

        for attempt in range(1, attempts + 1):
            model = _get_model_for_attempt(attempt)
            url = f"{GEMINI_BASE}/{model}:generateContent"
            log.detail(f"    Attempt {attempt}/{attempts} - model: {model}")
            try:
                await pace_requests()
                response = await client.post(
                    url,
                    headers={"x-goog-api-key": api_key},
                    json={"contents": current_contents, "generationConfig": gen_cfg},
                    timeout=300.0,
                )
                if response.status_code == 429:
                    # Rate limited: move to the next model at once rather than
                    # waiting on a model that is already out of quota.
                    log.attempt(attempt, attempts, f"{model}: rate limited")
                    await backoff_before_retry(attempt, attempts)
                    continue
                response.raise_for_status()
                raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                result = json_repair.loads(raw)
                normalized = _normalize_result(result, chunk)
                if not normalized:
                    raise ValueError("response failed ID validation")
                log.chunk_success(chunk_num, len(normalized))
                translated.update(normalized)
                _notify_progress(progress_callback, translated)
                contents.append({"role": "user", "parts": [{"text": turn_text}]})
                contents.append({"role": "model", "parts": [{"text": raw}]})
                log.advance_progress()
                break
            except Exception as e:
                log.attempt(attempt, attempts, f"{model}: {e}")
                await backoff_before_retry(attempt, attempts)
        else:
            failures.append(chunk_num)
            log.chunk_fail(chunk_num, f"after {attempts} attempts across "
                                      f"{len(ladder)} model(s)")
            log.update_progress(description=f"Translating ({len(failures)} failed)")

    return translated



# ── Full Context Mode ─────────────────────────────────────────────────────────

async def translate_full_context(client: httpx.AsyncClient, chunks: list,
                                 full_payload: dict, api_key: str,
                                 show_name: str = "", source_lang: str = "english",
                                 target_lang: str = "arabic", progress_callback=None) -> dict:
    """Translate using full_context mode: full blob sent every request."""
    from .blob import estimate_output_tokens

    translated = {}
    failures: list = []

    for chunk_num, chunk in enumerate(chunks, 1):
        await pace_requests()

        est = estimate_output_tokens(chunk)
        ladder = model_ladder()
        attempts = effective_attempts()
        log.chunk_status(chunk_num, len(chunks), len(chunk), est,
                         f"{ladder[0]} (+{len(ladder) - 1} fallback, full context)"
                         if len(ladder) > 1 else f"{ladder[0]} (full context)")

        keys = list(chunk.keys())
        prompt = _build_full_context_prompt(keys, full_payload, show_name, source_lang, target_lang)

        for attempt in range(1, attempts + 1):
            model = _get_model_for_attempt(attempt)
            log.detail(f"    Attempt {attempt}/{attempts} - model: {model}")
            result = await _call_gemini(client, prompt, api_key, model=model, attempt=attempt)
            if result:
                expected = {key: full_payload[key] for key in keys}
                normalized = _normalize_result(result, expected)
                if normalized:
                    log.chunk_success(chunk_num, len(normalized))
                    translated.update(normalized)
                    _notify_progress(progress_callback, translated)
                    log.advance_progress()
                    break
                log.attempt(attempt, attempts, f"{model}: response failed ID validation")
                await backoff_before_retry(attempt, attempts)
            else:
                log.attempt(attempt, attempts, f"{model}: failed")
                await backoff_before_retry(attempt, attempts)
        else:
            failures.append(chunk_num)
            log.chunk_fail(chunk_num, f"after {attempts} attempts across "
                                      f"{len(ladder)} model(s)")
            log.update_progress(description=f"Translating ({len(failures)} failed)")

    return translated



# ── Retry Missing Keys ────────────────────────────────────────────────────────

def build_retry_batches(missing_keys: set, full_payload: dict, context_lines: int = 3) -> list:
    """Build retry chunks for missing keys with neighboring context."""
    all_keys = list(full_payload.keys())
    key_to_idx = {k: i for i, k in enumerate(all_keys)}
    max_lines = max(1, cfg.get("MAX_LINES_PER_CHUNK", 1000))

    missing_sorted = sorted(missing_keys, key=lambda k: key_to_idx.get(k, 0))
    result = []
    current_batch_keys = []
    current_context_set = set()

    for k in missing_sorted:
        idx = key_to_idx.get(k, 0)
        new_context = set()
        for offset in range(-context_lines, context_lines + 1):
            neighbor_idx = idx + offset
            if 0 <= neighbor_idx < len(all_keys):
                new_context.add(all_keys[neighbor_idx])

        combined_context = current_context_set | new_context
        total_lines = len(combined_context)

        if total_lines > max_lines and current_batch_keys:
            context = {ck: full_payload[ck] for ck in
                       sorted(current_context_set, key=lambda x: key_to_idx.get(x, 0))}
            result.append({"translate_keys": current_batch_keys, "context": context})
            current_batch_keys = [k]
            current_context_set = new_context
        else:
            current_batch_keys.append(k)
            current_context_set = combined_context

    if current_batch_keys:
        context = {ck: full_payload[ck] for ck in
                   sorted(current_context_set, key=lambda x: key_to_idx.get(x, 0))}
        result.append({"translate_keys": current_batch_keys, "context": context})

    return result


async def retry_missing(client: httpx.AsyncClient, missing_keys: set,
                        full_payload: dict, api_key: str, show_name: str = "",
                        source_lang: str = "english", target_lang: str = "arabic",
                        recovered_callback=None) -> dict:
    """Retry translation of missing keys with context + model cycling."""
    recovered = {}
    max_retries = cfg.get("MAX_FAILED_CHUNKS", 5)
    remaining = set(missing_keys)

    for retry_round in range(1, max_retries + 1):
        if not remaining:
            break

        log.info(f"\n  Retry round {retry_round}/{max_retries} - {len(remaining)} lines missing")
        batches = build_retry_batches(remaining, full_payload, context_lines=3)
        log.detail(f"    Split into {len(batches)} retry batch(es)")

        # Retries get their own bar. Without one this phase, often the longest,
        # showed no movement at all.
        log.start_progress(f"Retry round {retry_round}/{max_retries}", total=len(batches))

        ladder = model_ladder()
        attempts = effective_attempts()

        for batch_num, batch in enumerate(batches, 1):
            log.detail(f"  RETRY {batch_num}/{len(batches)} - "
                       f"{len(batch['translate_keys'])} lines - "
                       f"{len(ladder)} model(s) available")

            prompt = _build_retry_prompt(
                batch["translate_keys"], batch["context"],
                show_name, source_lang, target_lang
            )

            # Start this round at a different point in the ladder so later rounds
            # do not open with the model that already failed.
            for attempt in range(1, attempts + 1):
                model = _get_model_for_attempt(attempt + retry_round - 1)
                log.detail(f"    Attempt {attempt}/{attempts} - model: {model}")
                result = await _call_gemini(client, prompt, api_key, model=model, attempt=attempt)
                if result:
                    expected = {key: full_payload[key] for key in batch["translate_keys"]}
                    normalized = _normalize_result(result, expected)
                    log.info(f"    Recovered {len(normalized)}/{len(batch['translate_keys'])} keys")
                    if normalized:
                        recovered.update(normalized)
                        remaining -= set(normalized.keys())
                        if recovered_callback:
                            recovered_callback(normalized)
                        break
                    log.attempt(attempt, attempts, f"{model}: response failed ID validation")
                else:
                    log.attempt(attempt, attempts, f"{model}: retry failed")
                await backoff_before_retry(attempt, attempts)

            log.advance_progress()

        log.finish_progress()

        if not remaining:
            log.success(f"  All lines recovered after {retry_round} retry round(s)!")
            break

    if remaining:
        log.warning(f"{len(remaining)} lines remain untranslated after retries")

    return recovered



# ── Main Translation Runner ───────────────────────────────────────────────────

async def run_translation(chunks: list, payload: dict, api_key: str,
                          show_name: str = "", source_lang: str = "english",
                          target_lang: str = "arabic", progress_callback=None) -> dict:
    """Run async translation using the configured mode, then retry missing."""
    mode = cfg.get("TRANSLATION_MODE", "chunked")
    translated = {}

    async with httpx.AsyncClient() as client:
        if mode == "multi_turn":
            translated = await translate_multi_turn(
                client, chunks, payload, api_key, show_name, source_lang, target_lang,
                progress_callback=progress_callback,
            )
        elif mode == "full_context":
            translated = await translate_full_context(
                client, chunks, payload, api_key, show_name, source_lang, target_lang,
                progress_callback=progress_callback,
            )
        else:
            translated = await translate_chunked(
                client, chunks, api_key, show_name, source_lang, target_lang,
                progress_callback=progress_callback,
            )

        # Retry missing
        all_keys = set()
        for ch in chunks:
            all_keys.update(ch.keys())
        missing = all_keys - set(translated.keys())

        if missing:
            # Stop the chunk-phase bar before retries. Leaving a finished bar
            # live freezes its elapsed clock and re-prints it on every log line.
            log.finish_progress()
            def _recovered_progress(partial):
                """Forward lines recovered during retries, merged into the whole.

                The caller expects the full picture each time, not just the
                delta, so files completed by a recovered line are written at
                once rather than waiting for the retry phase to end.
                """
                translated.update(partial)
                _notify_progress(progress_callback, translated)

            recovered = await retry_missing(
                client, missing, payload, api_key, show_name, source_lang, target_lang,
                recovered_callback=_recovered_progress,
            )
            translated.update(recovered)

    return translated
