"""Full translation pipeline: parse → blob → translate → reassemble.

Self-contained CLI version — no web server, no DB, no background tasks.
Uses Gemini API directly with retry logic.
"""
import asyncio
import json
import re
import time
from pathlib import Path

import httpx
import pysubs2

from .config import cfg

# ── Constants ─────────────────────────────────────────────────────────────────
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
RLI = "\u2067"
PDI = "\u2069"
SEP = "=" * 60



# ── Subtitle Parsing (from srt_pre.py) ────────────────────────────────────────
_ALL_TAGS_RE = re.compile(r"\{[^}]*\}")


def _get_preserve_tags() -> list:
    raw = cfg.get("PRESERVE_TAGS", "pos, an, move, fad, fade;")
    raw = raw.rstrip(";").strip()
    return [t.strip() for t in raw.split(",") if t.strip()]


def _extract_pos_tags(raw: str) -> tuple:
    """Extract preserved ASS tags. Returns (pos_tags_string, cleaned_text)."""
    preserve_list = _get_preserve_tags()
    if not preserve_list:
        clean = _ALL_TAGS_RE.sub("", raw)
        clean = clean.replace(r"\N", "\n").replace(r"\n", "\n").strip()
        return "", clean

    tag_pattern = "|".join(re.escape(t) for t in preserve_list)
    preserve_re = re.compile(r"\\(?:" + tag_pattern + r")(?:\([^)]*\)|[^\\}]*)")

    preserved = []
    for match in re.finditer(r"\{([^}]*)\}", raw):
        block_content = match.group(1)
        found = preserve_re.findall(block_content)
        if found:
            preserved.extend(found)

    pos_string = "{" + "".join(preserved) + "}" if preserved else ""
    clean = _ALL_TAGS_RE.sub("", raw)
    clean = clean.replace(r"\N", "\n").replace(r"\n", "\n").strip()
    return pos_string, clean



def _clean_event_text(raw: str) -> str:
    text = _ALL_TAGS_RE.sub("", raw or "")
    text = text.replace(r"\N", "\n").replace(r"\n", "\n")
    return text.strip()


def _should_drop(text: str) -> bool:
    if not text:
        return True
    if len(text) == 1 and not text.isdigit():
        return True
    return False


def parse_subtitle_file(path, keep_styles: list = None) -> list:
    """Parse SRT/ASS file → list of {text, start, end, pos_tags}."""
    subs = pysubs2.SSAFile.load(str(path))
    cues = []
    preserve_tags = _get_preserve_tags()

    for event in subs:
        if keep_styles is not None and hasattr(event, "style"):
            if event.style not in keep_styles:
                continue
        if preserve_tags and event.text:
            pos_tags, clean = _extract_pos_tags(event.text)
        else:
            pos_tags = ""
            clean = _clean_event_text(event.text)
        if _should_drop(clean):
            continue
        cues.append({
            "text": clean,
            "start": event.start,
            "end": event.end,
            "pos_tags": pos_tags,
        })
    return cues


def get_styles_from_files(paths: list) -> list:
    """Return unique ASS style names across all files."""
    all_styles = set()
    for p in paths:
        try:
            subs = pysubs2.SSAFile.load(str(p))
            all_styles.update(subs.styles.keys())
        except Exception:
            pass
    return sorted(all_styles)



# ── Blob Construction (from blob.py) ──────────────────────────────────────────

def build_blob(files: list, keep_styles: list = None):
    """Build deduped translation blob.

    Returns (meta, payload, stats).
    meta:    {tag: {file_idx, file_path, block_idx, start, end, text, rep, pos_tags}}
    payload: {rep_tag: text}  -- unique lines only
    stats:   {total, unique, collapsed, pct}
    """
    meta = {}
    payload = {}
    text_to_rep = {}
    total = 0

    for file_idx, fpath in enumerate(files, start=1):
        file_id = f"{file_idx:02d}"
        try:
            cues = parse_subtitle_file(fpath, keep_styles=keep_styles)
        except Exception as e:
            print(f"  Failed to parse {fpath.name}: {e}")
            continue

        for block_num, cue in enumerate(cues, start=1):
            tag = f"{file_id}{block_num:04d}"
            text = cue["text"]
            total += 1

            rep = text_to_rep.get(text)
            if rep is None:
                rep = tag
                text_to_rep[text] = tag
                payload[tag] = text

            meta[tag] = {
                "file_idx": file_idx,
                "file_path": str(fpath),
                "block_idx": block_num,
                "start": cue["start"],
                "end": cue["end"],
                "text": text,
                "rep": rep,
                "pos_tags": cue.get("pos_tags", ""),
            }
        print(f"  [{file_id}] {fpath.name} -> {len(cues)} cues")

    unique = len(payload)
    collapsed = total - unique
    pct = round(collapsed / total * 100) if total else 0
    stats = {"total": total, "unique": unique, "collapsed": collapsed, "pct": pct}
    return meta, payload, stats



def estimate_output_tokens(chunk: dict) -> int:
    """Rough Arabic output-token estimate."""
    total_chars = sum(len(v) for v in chunk.values())
    return int(total_chars / 3 * 1.5)


def split_blob(payload: dict) -> list:
    """Split unique payload into chunks <= MAX_LINES_PER_CHUNK."""
    max_lines = max(1, cfg.get("MAX_LINES_PER_CHUNK", 1000))
    items = list(payload.items())
    total = len(items)
    if total <= max_lines:
        return [dict(items)]
    num_chunks = (total + max_lines - 1) // max_lines
    chunk_size = (total + num_chunks - 1) // num_chunks
    chunks = []
    for i in range(0, total, chunk_size):
        chunks.append(dict(items[i:i + chunk_size]))
    return chunks


def expand_translations(translated_unique: dict, meta: dict) -> dict:
    """Fan unique-line translations back to every cue via meta['rep']."""
    out = {}
    for tag, m in meta.items():
        arabic = translated_unique.get(m["rep"])
        if arabic is not None:
            out[tag] = arabic
    return out



# ── Gemini API Translation (from ai.py) ───────────────────────────────────────
_last_api_call = 0.0


async def _enforce_cooldown():
    global _last_api_call
    cooldown = cfg.get("PARALLEL_COOLDOWN", 60)
    elapsed = time.time() - _last_api_call
    if _last_api_call > 0 and elapsed < cooldown:
        wait = cooldown - elapsed
        print(f"  Cooldown: waiting {wait:.0f}s...")
        await asyncio.sleep(wait)
    _last_api_call = time.time()


def _build_prompt(chunk: dict, show_name: str = "") -> str:
    template = cfg.get("PROMPT_TEMPLATE", "")
    if template and "{json_blob}" in template:
        name = show_name or "Unknown"
        return template.replace("{show_name}", name).replace(
            "{json_blob}", json.dumps(chunk, ensure_ascii=False)
        )
    return (
        "You are a professional English to Arabic subtitle translator.\n"
        "Translate each value in the following JSON object to Arabic.\n"
        "Return a valid JSON object with the EXACT same keys and ONLY Arabic values.\n"
        "No extra keys or explanation.\n\n"
        + json.dumps(chunk, ensure_ascii=False)
    )


def _generation_config() -> dict:
    gen = {"temperature": 0.1, "responseMimeType": "application/json"}
    max_out = cfg.get("GEMINI_MAX_OUTPUT_TOKENS", 0)
    if max_out and max_out > 0:
        gen["maxOutputTokens"] = max_out
    return gen



async def _translate_chunk(client: httpx.AsyncClient, chunk: dict, chunk_num: int,
                           total: int, api_key: str, show_name: str = "") -> dict | None:
    """Translate one chunk via Gemini. Returns {key: arabic} or None."""
    import json_repair

    model = cfg.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"{GEMINI_BASE}/{model}:generateContent"
    prompt = _build_prompt(chunk, show_name)
    gen_cfg = _generation_config()

    retry_attempts = cfg.get("RETRY_ATTEMPTS", 5)
    retry_cooldown = cfg.get("RETRY_COOLDOWN", 10)

    est = estimate_output_tokens(chunk)
    print(f"  CHUNK {chunk_num}/{total} - {len(chunk)} lines, ~{est} tokens - model: {model}")

    for attempt in range(1, retry_attempts + 1):
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
                wait = retry_cooldown * attempt
                print(f"    Attempt {attempt}/{retry_attempts} - rate limited, waiting {wait}s...")
                await asyncio.sleep(wait)
                continue
            response.raise_for_status()
            raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            result = json_repair.loads(raw)
            print(f"    SUCCESS ({len(result)} keys)")
            return result

        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            print(f"    Attempt {attempt}/{retry_attempts} - HTTP {code}")
            if attempt < retry_attempts:
                await asyncio.sleep(retry_cooldown)

        except Exception as e:
            print(f"    Attempt {attempt}/{retry_attempts} - ERROR: {e}")
            if attempt < retry_attempts:
                await asyncio.sleep(retry_cooldown)

    print(f"    CHUNK {chunk_num} FAILED after {retry_attempts} attempts")
    return None



async def _translate_retry_with_context(client: httpx.AsyncClient, translate_keys: list,
                                        context: dict, retry_num: int, total_retries: int,
                                        api_key: str, show_name: str = "") -> dict | None:
    """Retry missing keys with surrounding context for better quality."""
    import json_repair

    model = cfg.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"{GEMINI_BASE}/{model}:generateContent"
    gen_cfg = _generation_config()
    retry_attempts = cfg.get("RETRY_ATTEMPTS", 5)
    retry_cooldown = cfg.get("RETRY_COOLDOWN", 10)

    prompt = (
        f"You are a professional English to Arabic subtitle translator.\n"
        f"Context: Subtitles from \"{show_name or 'Unknown'}\".\n\n"
        f"Below is a section of dialogue. Translate ONLY the keys listed below.\n"
        f"Keys to translate: {json.dumps(translate_keys)}\n\n"
        f"Return a valid JSON object with ONLY those keys and their Arabic translations.\n"
        f"Do NOT translate or include any other keys.\n\n"
        f"Dialogue section:\n"
        f"{json.dumps(context, ensure_ascii=False)}"
    )

    print(f"  RETRY {retry_num}/{total_retries} - {len(translate_keys)} lines")

    for attempt in range(1, retry_attempts + 1):
        try:
            await _enforce_cooldown()
            response = await client.post(
                url,
                headers={"x-goog-api-key": api_key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": gen_cfg},
                timeout=300.0,
            )
            if response.status_code == 429:
                await asyncio.sleep(retry_cooldown * attempt)
                continue
            response.raise_for_status()
            raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            result = json_repair.loads(raw)
            filtered = {k: v for k, v in result.items() if k in translate_keys}
            print(f"    Recovered {len(filtered)}/{len(translate_keys)} keys")
            return filtered
        except Exception as e:
            print(f"    Retry attempt {attempt} failed: {e}")
            if attempt < retry_attempts:
                await asyncio.sleep(retry_cooldown)

    return None



def _build_retry_with_context(missing_keys: set, full_payload: dict, context_lines: int = 3) -> list:
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
            context = {ck: full_payload[ck] for ck in sorted(current_context_set, key=lambda x: key_to_idx.get(x, 0))}
            result.append({"translate_keys": current_batch_keys, "context": context})
            current_batch_keys = [k]
            current_context_set = new_context
        else:
            current_batch_keys.append(k)
            current_context_set = combined_context

    if current_batch_keys:
        context = {ck: full_payload[ck] for ck in sorted(current_context_set, key=lambda x: key_to_idx.get(x, 0))}
        result.append({"translate_keys": current_batch_keys, "context": context})

    return result



# ── Output Reassembly (from sub_post.py) ──────────────────────────────────────

def _wrap_rtl(text: str) -> str:
    """Wrap each line with RLI+PDI pair."""
    lines = text.split("\n")
    return "\n".join(RLI + line + PDI for line in lines)


def _resolve_output_path(base_path) -> Path:
    """Build output path with .ar extension."""
    fpath = Path(str(base_path))
    stem = fpath.stem
    for suffix in (".en", ".eng", ".en.hi"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break

    source_ext = fpath.suffix.lower()
    ext = ".ar.ass" if source_ext in (".ass", ".ssa") else ".ar.srt"
    out_path = fpath.with_name(stem + ext)

    if cfg.get("FILE_CONFLICT", "overwrite") == "rename":
        counter = 1
        base_stem = stem
        while out_path.exists():
            out_path = fpath.with_name(f"{base_stem}.ar_{counter}" + ext.replace(".ar", ""))
            counter += 1
    return out_path


def _build_ass_output(blocks: list) -> str:
    """Build ASS file from translated blocks."""
    subs = pysubs2.SSAFile()

    style = pysubs2.SSAStyle()
    style.fontname = cfg.get("FONT_NAME", "Amiri")
    style.fontsize = cfg.get("FONT_SIZE", 40)
    style.encoding = 1
    style.alignment = cfg.get("FONT_ALIGNMENT", 2)
    style.outline = cfg.get("FONT_OUTLINE", 1)
    style.shadow = cfg.get("FONT_SHADOW", 0)
    style.bold = False
    style.italic = False
    style.marginl = cfg.get("FONT_MARGIN_L", 20)
    style.marginr = cfg.get("FONT_MARGIN_R", 20)
    style.marginv = cfg.get("FONT_MARGIN_V", 30)
    subs.styles["Default"] = style

    for block in blocks:
        event = pysubs2.SSAEvent()
        event.start = block["start"]
        event.end = block["end"]
        event.text = block["text"]
        event.style = "Default"
        subs.append(event)

    return subs.to_string("ass")



def reassemble_files(translated_blob: dict, meta: dict, files: list):
    """Write translated output files.

    Returns (completed_names, warnings_list).
    """
    file_cues = {i + 1: [] for i in range(len(files))}
    for tag, m in meta.items():
        file_cues[m["file_idx"]].append((tag, m))

    completed, warnings = [], []

    for file_idx, cues in file_cues.items():
        fpath = files[file_idx - 1]
        if not cues:
            print(f"  No cues for {fpath.name} - skipping")
            warnings.append(f"{fpath.name}: no cues found")
            continue

        cues.sort(key=lambda x: x[1]["block_idx"])
        untranslated = []
        blocks = []

        for tag, m in cues:
            arabic = translated_blob.get(tag)
            pos_tags = m.get("pos_tags", "")

            if arabic is not None:
                text = pos_tags + _wrap_rtl(arabic) if pos_tags else _wrap_rtl(arabic)
            else:
                text = pos_tags + _wrap_rtl(m["text"]) if pos_tags else _wrap_rtl(m["text"])
                untranslated.append((tag, m["text"]))

            blocks.append({
                "start": m["start"],
                "end": m["end"],
                "text": text,
                "block_idx": m["block_idx"],
            })

        out_path = _resolve_output_path(fpath)
        is_ass = out_path.suffix == ".ass"

        if is_ass:
            ass_content = _build_ass_output(blocks)
            out_path.write_text(ass_content, encoding="utf-8")
        else:
            srt_lines = []
            for i, block in enumerate(blocks, start=1):
                s = pysubs2.time.ms_to_str(block["start"], fractions=True).replace(".", ",")
                e = pysubs2.time.ms_to_str(block["end"], fractions=True).replace(".", ",")
                srt_lines.append(f"{i}\n{s} --> {e}\n{block['text']}\n")
            out_path.write_text("\n".join(srt_lines), encoding="utf-8")

        if untranslated:
            print(f"  {out_path.name}: {len(blocks) - len(untranslated)}/{len(blocks)} translated, "
                  f"{len(untranslated)} kept as original")
            warnings.append(f"{out_path.name}: {len(untranslated)} lines untranslated")
        else:
            print(f"  {out_path.name}: {len(blocks)} cues (fully translated)")

        completed.append(out_path.name)

    return completed, warnings



# ── Show Name Detection ───────────────────────────────────────────────────────

def _detect_show_name(files: list) -> str:
    """Auto-detect show name from filenames."""
    names = [f.stem for f in files]
    if not names:
        return ""
    patterns = [r'^(.+?)\s*-\s*S\d', r'^(.+?)\s*-\s*E\d', r'^(.+?)\s+S\d']
    for pattern in patterns:
        matches = []
        for name in names:
            m = re.match(pattern, name)
            if m:
                matches.append(m.group(1).strip())
        if matches:
            from collections import Counter
            return Counter(matches).most_common(1)[0][0]
    if len(names) == 1:
        return names[0].split(' - ')[0].strip() if ' - ' in names[0] else names[0]
    prefix = names[0]
    for name in names[1:]:
        while not name.startswith(prefix) and prefix:
            prefix = prefix[:-1]
    return prefix.strip().rstrip('-').strip()



# ── Main Pipeline Orchestrator ────────────────────────────────────────────────

def run_translate(file_paths: list, keep_styles: list = None, show_name: str = "") -> None:
    """Run the full translation pipeline synchronously (calls async internally).

    Steps:
      1. Parse subtitle files → build deduped blob
      2. Split into chunks
      3. Translate via Gemini API
      4. Retry missing lines with context
      5. Reassemble output files
    """
    api_key = cfg.get("GEMINI_API_KEY", "")
    if not api_key:
        import os
        api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: No API key. Set GEMINI_API_KEY in settings.conf or environment.")
        return

    files = [Path(p) for p in file_paths]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        print(f"ERROR: Files not found: {missing}")
        return

    print(SEP)
    print(f"TRANSLATE - {len(files)} file(s)")
    for i, f in enumerate(files, 1):
        print(f"  [{i:02d}] {f.name}  ({f.stat().st_size / 1024:.1f} KB)")

    # Phase 1: Build blob
    print(SEP)
    print("PHASE 1 - Building blob...")
    meta, payload, stats = build_blob(files, keep_styles=keep_styles)
    if stats["total"] == 0:
        print("ERROR: No dialogue cues found in selected files.")
        return
    max_blob = cfg.get("MAX_BLOB_LINES", 50000)
    if stats["total"] > max_blob:
        print(f"ERROR: Too many cues ({stats['total']} > {max_blob}). Select fewer files.")
        return
    print(f"DEDUP: {stats['total']} total -> {stats['unique']} unique "
          f"({stats['collapsed']} collapsed, ~{stats['pct']}% fewer tokens)")

    # Phase 2: Split into chunks
    print(SEP)
    print("PHASE 2 - Splitting into chunks...")
    chunks = split_blob(payload)
    print(f"Split into {len(chunks)} chunk(s)")
    total_tokens = 0
    for i, ch in enumerate(chunks, 1):
        est = estimate_output_tokens(ch)
        total_tokens += est
        print(f"  Chunk {i}: {len(ch)} lines, ~{est} output tokens")
    print(f"Total estimated output tokens: {total_tokens}")

    # Detect show name
    if not show_name:
        show_name = _detect_show_name(files)
    print(f"Show name: {show_name}")

    # Phase 3: Translate
    print(SEP)
    print("PHASE 3 - Translating...")
    translated_unique = asyncio.run(_run_translation(chunks, payload, api_key, show_name))

    # Phase 4: Reassemble
    print(SEP)
    print("PHASE 4 - Reassembling output files...")
    translated_blob = expand_translations(translated_unique, meta)
    completed, warnings = reassemble_files(translated_blob, meta, files)

    print(SEP)
    print(f"COMPLETE - {len(completed)} files written"
          + (f", {len(warnings)} with warnings" if warnings else ""))
    for f in completed:
        print(f"  done: {f}")
    for w in warnings:
        print(f"  warning: {w}")



async def _run_translation(chunks: list, payload: dict, api_key: str, show_name: str) -> dict:
    """Async translation loop: translate chunks then retry missing."""
    translated_unique = {}
    parallel = max(1, cfg.get("PARALLEL_CHUNKS", 1))

    async with httpx.AsyncClient() as client:
        # Translate all chunks
        for batch_start in range(0, len(chunks), parallel):
            batch = chunks[batch_start:batch_start + parallel]

            async def translate_one(chunk, idx):
                return await _translate_chunk(client, chunk, idx, len(chunks), api_key, show_name)

            tasks = [translate_one(ch, batch_start + i + 1) for i, ch in enumerate(batch)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, result in enumerate(results):
                chunk_idx = batch_start + i + 1
                if isinstance(result, Exception):
                    print(f"  Chunk {chunk_idx} - ERROR: {result}")
                elif result:
                    translated_unique.update(result)
                else:
                    print(f"  Chunk {chunk_idx} - FAILED")

        # Retry missing keys
        all_payload_keys = set()
        for ch in chunks:
            all_payload_keys.update(ch.keys())
        global_missing = all_payload_keys - set(translated_unique.keys())
        max_retries = cfg.get("MAX_FAILED_CHUNKS", 5)
        retry_round = 0

        while global_missing and retry_round < max_retries:
            retry_round += 1
            print(f"\n  Retry round {retry_round}/{max_retries} - {len(global_missing)} lines missing")

            retry_batches = _build_retry_with_context(global_missing, payload, context_lines=3)
            print(f"    Split into {len(retry_batches)} retry batch(es)")

            for batch_num, batch in enumerate(retry_batches, 1):
                result = await _translate_retry_with_context(
                    client, batch["translate_keys"], batch["context"],
                    batch_num, len(retry_batches), api_key, show_name
                )
                if result:
                    translated_unique.update(result)
                    recovered = global_missing & set(result.keys())
                    global_missing -= recovered

            if not global_missing:
                print(f"  All lines recovered after {retry_round} retry round(s)!")
                break

        if global_missing:
            print(f"\n  WARNING: {len(global_missing)} lines remain untranslated after retries")

    return translated_unique
