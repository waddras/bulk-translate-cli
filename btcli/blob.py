"""Blob construction: build deduplicated translation blob, split into chunks.

The blob is a dict of {tag: text} where:
  - tag format: FFLLLL (FF = file index, LLLL = line index within file)
  - Duplicate lines across files share the same "rep" tag (translated once)
"""
from pathlib import Path

from .config import cfg
from .srt_pre import parse_subtitle_file


def build_blob(files: list, keep_styles: list | None = None):
    """Build deduped translation blob from subtitle files.

    Args:
        files: list of Path objects to subtitle files
        keep_styles: if provided, only keep events with these ASS style names

    Returns:
        (meta, payload, stats)
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
        fpath = Path(fpath)
        try:
            cues = parse_subtitle_file(fpath, keep_styles=keep_styles)
        except Exception as e:
            print(f"  Failed to parse {fpath.name}: {e}")
            continue

        for block_num, cue in enumerate(cues, start=1):
            tag = f"{file_id}{block_num:04d}"
            text = cue["text"]
            total += 1

            # Deduplication: same text → same rep tag
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


def split_blob(payload: dict) -> list:
    """Split unique payload into chunks <= MAX_LINES_PER_CHUNK.

    Distributes lines evenly across chunks.

    Returns:
        List of dicts, each a subset of payload.
    """
    max_lines = max(1, cfg.get("MAX_LINES_PER_CHUNK", 1000))
    items = list(payload.items())
    total = len(items)

    if total <= max_lines:
        return [dict(items)]

    # Even distribution
    num_chunks = (total + max_lines - 1) // max_lines
    chunk_size = (total + num_chunks - 1) // num_chunks
    chunks = []
    for i in range(0, total, chunk_size):
        chunks.append(dict(items[i:i + chunk_size]))
    return chunks


def expand_translations(translated_unique: dict, meta: dict) -> dict:
    """Fan unique-line translations back to every cue via meta['rep'].

    Args:
        translated_unique: {rep_tag: translated_text}
        meta: full meta dict from build_blob

    Returns:
        {tag: translated_text} for every tag that has a translation
    """
    out = {}
    for tag, m in meta.items():
        translated = translated_unique.get(m["rep"])
        if translated is not None:
            out[tag] = translated
    return out


def estimate_output_tokens(chunk: dict) -> int:
    """Rough output-token estimate (for reporting).

    Assumes ~3 chars per token in source, ~1.5x expansion for Arabic.
    """
    total_chars = sum(len(v) for v in chunk.values())
    return int(total_chars / 3 * 1.5)
