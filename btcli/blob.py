"""Turning subtitle files into the payload sent to the API, and back again.

THE TWO DICTS
    ``build_blob`` returns *meta* and *payload*, and the distinction matters:

    meta     every cue in every file, keyed by its own tag. This is the record
             used to rebuild output files, so it holds timing, style and
             positioning as well as text.
    payload  only the lines that actually need translating — one entry per
             *unique* text. This is what costs quota.

TAGS
    ``FFLLLL`` — a two-digit file index and the cue's four-digit position within
    that file. So ``030127`` is the 127th cue of the third file.

DEDUPLICATION
    Cues with identical text share a single "representative" tag, recorded as
    ``meta[tag]["rep"]``. Only representatives go in *payload*; the translation is
    fanned back out to every cue by ``expand_translations``. On dialogue-heavy
    series this removes a large fraction of the cost, since "Yes.", "Thank you."
    and character names recur constantly.

    CONSEQUENCE WORTH KNOWING: because a repeated line gets no payload entry of
    its own, **payload tags are not contiguous**. A file whose cues 4, 6 and 8
    are repeats yields tags ...0003, 0005, 0007, 0009. The model therefore
    receives a numbered list with holes in it, and has been observed inventing
    entries to fill them — which ``ai._normalize_result`` then rejects as
    unexpected IDs, costing a retry. See docs/PROJECT-STATE.md, open item 3.

CHUNKING
    ``split_blob`` divides the payload by line count; ``split_blob_by_files``
    divides it by whole source files instead, for ``--files-per-call``.
"""
from __future__ import annotations

from pathlib import Path

from .config import cfg
from .logger import log
from .srt_pre import parse_subtitle_file


def build_blob(files: list, keep_styles: list | None = None):
    """Read the files and build the meta record plus the deduplicated payload.

    Args:
        files: subtitle file paths, in the order their indexes are assigned
        keep_styles: if given, only cues with these ASS style names are included,
            which is how passthrough styles are excluded from translation

    Returns:
        (meta, payload, stats)
        meta:    {tag: {file_idx, file_path, block_idx, start, end, text, rep,
                        pos_tags, style}} — every cue
        payload: {rep_tag: text} — unique lines only, what gets sent
        stats:   {total, unique, collapsed, pct}

    A file that fails to parse is reported and skipped rather than aborting the
    batch, so one bad file does not cost a whole season. Note this still consumes
    its file index, keeping tags stable for the files that did parse.
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
            log.detail(f"  Failed to parse {fpath.name}: {e}")
            continue

        for block_num, cue in enumerate(cues, start=1):
            tag = f"{file_id}{block_num:04d}"
            text = cue["text"]
            total += 1

            # Deduplication. The first cue to carry a given text becomes its
            # representative and is the only one added to the payload; later
            # cues with the same text just point at it. This is what leaves
            # gaps in the payload tag sequence — see the module docstring.
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
                "style": cue.get("style", "Default"),
            }

        log.item(f"[{file_id}] {fpath.name} → {len(cues)} cues")

    unique = len(payload)
    collapsed = total - unique
    pct = round(collapsed / total * 100) if total else 0
    stats = {"total": total, "unique": unique, "collapsed": collapsed, "pct": pct}
    return meta, payload, stats


def split_blob(payload: dict) -> list:
    """Split the payload into chunks of at most MAX_LINES_PER_CHUNK.

    Sizes are evened out rather than filling each chunk to the maximum and
    leaving a small remainder: 1100 lines with a 1000 limit becomes 550 + 550,
    not 1000 + 100. Two similar requests fail and retry more predictably than
    one full and one nearly empty.

    Returns a list of dicts, each a subset of payload.
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
    """Fan translations of unique lines back out to every cue that used them.

    The inverse of the dedup step in ``build_blob``: one translated
    representative fills in every cue sharing that source text, across all files
    in the batch.

    Args:
        translated_unique: {rep_tag: translated_text}
        meta: the full meta dict from build_blob

    Returns:
        {tag: translated_text} for every cue whose representative was translated.
        Cues whose representative is still missing are simply absent, which is
        how partial results stay safe to write.
    """
    out = {}
    for tag, m in meta.items():
        translated = translated_unique.get(m["rep"])
        if translated is not None:
            out[tag] = translated
    return out


def estimate_output_tokens(chunk: dict) -> int:
    """Rough output-token estimate, for reporting and --dry-run only.

    Assumes ~3 characters per token in the source and ~1.5x expansion into
    Arabic. Deliberately crude: it exists to give an order of magnitude before
    committing quota, not to predict billing. Nothing branches on it.
    """
    total_chars = sum(len(v) for v in chunk.values())
    return int(total_chars / 3 * 1.5)



def split_blob_by_files(payload: dict, meta: dict, files_per_call: int) -> list:
    """Split the payload by whole source files instead of by line count.

    Unlike :func:`split_blob`, this deliberately ignores MAX_LINES_PER_CHUNK —
    the point of ``--files-per-call`` is fewer, larger requests, which keeps a
    whole episode in one context and cuts the request count against a tight RPD.

    A representative key is attributed to the file where that unique text *first
    appeared*, so a line shared between episodes is translated with the earlier
    one. Consecutive groups of ``files_per_call`` file indexes then form calls.

    Empty groups are dropped, which happens when every line in a file was a
    repeat of something already covered by an earlier file.
    """
    files_per_call = max(1, int(files_per_call))
    rep_file = {}
    max_file_idx = 0
    for item in meta.values():
        max_file_idx = max(max_file_idx, item["file_idx"])
        rep_file.setdefault(item["rep"], item["file_idx"])

    chunks = []
    for first_idx in range(1, max_file_idx + 1, files_per_call):
        last_idx = first_idx + files_per_call - 1
        chunk = {
            key: text for key, text in payload.items()
            if first_idx <= rep_file.get(key, 0) <= last_idx
        }
        if chunk:
            chunks.append(chunk)
    return chunks
