"""Gemini API translation: chunked, multi_turn, and full_context modes.

Translation modes:
  - chunked (default): independent chunks, parallel batches with cooldown
  - multi_turn: full blob as system context, chunks as conversation turns
  - full_context: full blob sent every request, only specific keys translated

Also handles:
  - Cooldown timer between API calls
  - Retry with context for missing keys
  - Model pool cycling on retries
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .config import cfg
from .logger import log

# ── Constants ─────────────────────────────────────────────────────────────────
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_last_api_call = 0.0


# ── Cooldown ──────────────────────────────────────────────────────────────────

async def _enforce_cooldown():
    """Wait for PARALLEL_COOLDOWN seconds between API calls."""
    global _last_api_call
    cooldown = cfg.get("PARALLEL_COOLDOWN", 60)
    elapsed = time.time() - _last_api_call
    if _last_api_call > 0 and elapsed < cooldown:
        wait = cooldown - elapsed
        log.cooldown(wait)
        await asyncio.sleep(wait)
    _last_api_call = time.time()



# ── Prompt Building ───────────────────────────────────────────────────────────

def _build_prompt(chunk: dict, show_name: str = "",
                  source_lang: str = "english", target_lang: str = "arabic") -> str:
    """Build translation prompt from template."""
    template = cfg.get("PROMPT_TEMPLATE", "")
    if template and "{json_blob}" in template:
        name = show_name or "Unknown"
        return (
            template
            .replace("{show_name}", name)
            .replace("{source_language}", source_lang)
            .replace("{target_language}", target_lang)
            .replace("{json_blob}", json.dumps(chunk, ensure_ascii=False))
        )
    return (
        f"You are a professional {source_lang} to {target_lang} subtitle translator.\n"
        f"Translate each value in the following JSON object to {target_lang}.\n"
        f"Return a valid JSON object with the EXACT same keys and ONLY {target_lang} values.\n"
        f"No extra keys or explanation.\n\n"
        + json.dumps(chunk, ensure_ascii=False)
    )


def _build_full_context_prompt(translate_keys: list, full_blob: dict,
                               show_name: str = "", source_lang: str = "english",
                               target_lang: str = "arabic") -> str:
    """Build prompt for full_context mode."""
    return (
        f"You are a professional {source_lang} to {target_lang} subtitle translator.\n"
        f"Context: Subtitles from \"{show_name or 'Unknown'}\".\n\n"
        f"Below is the FULL dialogue. Translate ONLY the keys listed below.\n"
        f"Keys to translate: {json.dumps(translate_keys)}\n\n"
        f"Return a valid JSON object with ONLY those keys and their {target_lang} translations.\n"
        f"Do NOT translate or include any other keys.\n\n"
        f"Full dialogue:\n"
        f"{json.dumps(full_blob, ensure_ascii=False)}"
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
        f"Return a valid JSON object with ONLY those keys and their {target_lang} translations.\n"
        f"Do NOT translate or include any other keys.\n\n"
        f"Dialogue section:\n"
        f"{json.dumps(context, ensure_ascii=False)}"
    )


def _generation_config() -> dict:
    """Build Gemini generation config."""
    gen = {"temperature": 0.1, "responseMimeType": "application/json"}
    max_out = cfg.get("GEMINI_MAX_OUTPUT_TOKENS", 0)
    if max_out and max_out > 0:
        gen["maxOutputTokens"] = max_out
    return gen



# ── Model Pool ────────────────────────────────────────────────────────────────

def _get_model_for_attempt(attempt: int) -> str:
    """Cycle through MODEL_POOL on retries."""
    pool = cfg.get("MODEL_POOL", [])
    primary = cfg.get("GEMINI_MODEL", "gemini-2.5-flash")
    if not pool:
        return primary
    if attempt <= 1:
        return primary
    idx = (attempt - 2) % len(pool)
    return pool[idx]


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
        await _enforce_cooldown()
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
                            target_lang: str = "arabic") -> dict:
    """Translate using chunked mode: independent chunks with retry."""
    from .blob import estimate_output_tokens

    translated = {}
    retry_attempts = cfg.get("RETRY_ATTEMPTS", 5)
    retry_cooldown = cfg.get("RETRY_COOLDOWN", 10)
    parallel = max(1, cfg.get("PARALLEL_CHUNKS", 1))
    total = len(chunks)

    for batch_start in range(0, total, parallel):
        batch = chunks[batch_start:batch_start + parallel]

        for i, chunk in enumerate(batch):
            chunk_num = batch_start + i + 1
            est = estimate_output_tokens(chunk)
            model = cfg.get("GEMINI_MODEL", "gemini-2.5-flash")
            log.chunk_status(chunk_num, total, len(chunk), est, model)

            prompt = _build_prompt(chunk, show_name, source_lang, target_lang)

            for attempt in range(1, retry_attempts + 1):
                result = await _call_gemini(client, prompt, api_key, attempt=attempt)
                if result:
                    log.chunk_success(chunk_num, len(result))
                    translated.update(result)
                    log.advance_progress()
                    break
                else:
                    log.attempt(attempt, retry_attempts, "failed")
                    if attempt < retry_attempts:
                        await asyncio.sleep(retry_cooldown * attempt)
            else:
                log.chunk_fail(chunk_num, f"after {retry_attempts} attempts")
                log.advance_progress()

    return translated



# ── Multi-Turn Mode ───────────────────────────────────────────────────────────

async def translate_multi_turn(client: httpx.AsyncClient, chunks: list,
                               full_payload: dict, api_key: str,
                               show_name: str = "", source_lang: str = "english",
                               target_lang: str = "arabic") -> dict:
    """Translate using multi_turn mode: full blob as context, chunks as turns."""
    from .blob import estimate_output_tokens
    import json_repair

    translated = {}
    model = cfg.get("GEMINI_MODEL", "gemini-2.5-flash")
    gen_cfg = _generation_config()
    retry_attempts = cfg.get("RETRY_ATTEMPTS", 5)
    retry_cooldown = cfg.get("RETRY_COOLDOWN", 10)

    context_text = (
        f"You are a professional {source_lang} to {target_lang} subtitle translator.\n"
        f"Context: Subtitles from \"{show_name or 'Unknown'}\".\n\n"
        f"Here is the full dialogue for reference:\n"
        f"{json.dumps(full_payload, ensure_ascii=False)}\n\n"
        f"I will send you subsets of keys to translate. For each subset:\n"
        f"- Return a valid JSON object with ONLY those keys and their {target_lang} translations\n"
        f"- Use the full context above for consistent tone and references\n"
        f"- No extra keys, no explanation, no markdown"
    )

    contents = [{"role": "user", "parts": [{"text": context_text}]}]

    for chunk_num, chunk in enumerate(chunks, 1):
        est = estimate_output_tokens(chunk)
        log.chunk_status(chunk_num, len(chunks), len(chunk), est, model)

        keys_to_translate = list(chunk.keys())
        turn_text = (
            f"Translate these keys:\n"
            f"{json.dumps(keys_to_translate)}\n\n"
            f"Values:\n"
            f"{json.dumps(chunk, ensure_ascii=False)}"
        )

        current_contents = contents + [{"role": "user", "parts": [{"text": turn_text}]}]
        url = f"{GEMINI_BASE}/{model}:generateContent"

        for attempt in range(1, retry_attempts + 1):
            try:
                await _enforce_cooldown()
                response = await client.post(
                    url,
                    headers={"x-goog-api-key": api_key},
                    json={"contents": current_contents, "generationConfig": gen_cfg},
                    timeout=300.0,
                )
                if response.status_code == 429:
                    log.attempt(attempt, retry_attempts, "rate limited")
                    if attempt < retry_attempts:
                        await asyncio.sleep(retry_cooldown * attempt)
                    continue
                response.raise_for_status()
                raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                result = json_repair.loads(raw)
                log.chunk_success(chunk_num, len(result))
                translated.update(result)
                contents.append({"role": "user", "parts": [{"text": turn_text}]})
                contents.append({"role": "model", "parts": [{"text": raw}]})
                log.advance_progress()
                break
            except Exception as e:
                log.attempt(attempt, retry_attempts, str(e))
                if attempt < retry_attempts:
                    await asyncio.sleep(retry_cooldown)
        else:
            log.chunk_fail(chunk_num, f"after {retry_attempts} attempts")
            log.advance_progress()

    return translated



# ── Full Context Mode ─────────────────────────────────────────────────────────

async def translate_full_context(client: httpx.AsyncClient, chunks: list,
                                 full_payload: dict, api_key: str,
                                 show_name: str = "", source_lang: str = "english",
                                 target_lang: str = "arabic") -> dict:
    """Translate using full_context mode: full blob sent every request."""
    from .blob import estimate_output_tokens

    translated = {}
    retry_attempts = cfg.get("RETRY_ATTEMPTS", 5)
    retry_cooldown = cfg.get("RETRY_COOLDOWN", 10)

    for chunk_num, chunk in enumerate(chunks, 1):
        est = estimate_output_tokens(chunk)
        model = cfg.get("GEMINI_MODEL", "gemini-2.5-flash")
        log.chunk_status(chunk_num, len(chunks), len(chunk), est, f"{model} (full context)")

        keys = list(chunk.keys())
        prompt = _build_full_context_prompt(keys, full_payload, show_name, source_lang, target_lang)

        for attempt in range(1, retry_attempts + 1):
            result = await _call_gemini(client, prompt, api_key, attempt=attempt)
            if result:
                filtered = {k: v for k, v in result.items() if k in keys}
                log.chunk_success(chunk_num, len(filtered))
                translated.update(filtered)
                log.advance_progress()
                break
            else:
                log.attempt(attempt, retry_attempts, "failed")
                if attempt < retry_attempts:
                    await asyncio.sleep(retry_cooldown * attempt)
        else:
            log.chunk_fail(chunk_num, f"after {retry_attempts} attempts")
            log.advance_progress()

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
                        source_lang: str = "english", target_lang: str = "arabic") -> dict:
    """Retry translation of missing keys with context + model cycling."""
    recovered = {}
    max_retries = cfg.get("MAX_FAILED_CHUNKS", 5)
    retry_cooldown = cfg.get("RETRY_COOLDOWN", 10)
    remaining = set(missing_keys)

    for retry_round in range(1, max_retries + 1):
        if not remaining:
            break

        log.info(f"\n  Retry round {retry_round}/{max_retries} - {len(remaining)} lines missing")
        batches = build_retry_batches(remaining, full_payload, context_lines=3)
        log.detail(f"    Split into {len(batches)} retry batch(es)")

        for batch_num, batch in enumerate(batches, 1):
            model = _get_model_for_attempt(retry_round + 1)
            log.detail(f"  RETRY {batch_num}/{len(batches)} - "
                       f"{len(batch['translate_keys'])} lines - model: {model}")

            prompt = _build_retry_prompt(
                batch["translate_keys"], batch["context"],
                show_name, source_lang, target_lang
            )

            retry_attempts = cfg.get("RETRY_ATTEMPTS", 5)
            for attempt in range(1, retry_attempts + 1):
                result = await _call_gemini(client, prompt, api_key, model=model, attempt=attempt)
                if result:
                    filtered = {k: v for k, v in result.items() if k in batch["translate_keys"]}
                    log.info(f"    Recovered {len(filtered)}/{len(batch['translate_keys'])} keys")
                    recovered.update(filtered)
                    remaining -= set(filtered.keys())
                    break
                else:
                    log.attempt(attempt, retry_attempts, "retry failed")
                    if attempt < retry_attempts:
                        await asyncio.sleep(retry_cooldown)

        if not remaining:
            log.success(f"  All lines recovered after {retry_round} retry round(s)!")
            break

    if remaining:
        log.warning(f"{len(remaining)} lines remain untranslated after retries")

    return recovered



# ── Main Translation Runner ───────────────────────────────────────────────────

async def run_translation(chunks: list, payload: dict, api_key: str,
                          show_name: str = "", source_lang: str = "english",
                          target_lang: str = "arabic") -> dict:
    """Run async translation using the configured mode, then retry missing."""
    mode = cfg.get("TRANSLATION_MODE", "chunked")
    translated = {}

    async with httpx.AsyncClient() as client:
        if mode == "multi_turn":
            translated = await translate_multi_turn(
                client, chunks, payload, api_key, show_name, source_lang, target_lang
            )
        elif mode == "full_context":
            translated = await translate_full_context(
                client, chunks, payload, api_key, show_name, source_lang, target_lang
            )
        else:
            translated = await translate_chunked(
                client, chunks, api_key, show_name, source_lang, target_lang
            )

        # Retry missing
        all_keys = set()
        for ch in chunks:
            all_keys.update(ch.keys())
        missing = all_keys - set(translated.keys())

        if missing:
            recovered = await retry_missing(
                client, missing, payload, api_key, show_name, source_lang, target_lang
            )
            translated.update(recovered)

    return translated
