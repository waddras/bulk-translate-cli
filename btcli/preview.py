"""Dry-run reporting: what a job would do, before any quota is spent.

A translation job can run for the better part of an hour and consume a day's
request allowance. This shows the shape of the work first: how many lines are
genuinely new after deduplication and the cache, how many requests that becomes,
and roughly how long the pacing alone will take.

Nothing here writes a file, calls the API, or touches the manifest.
"""
from __future__ import annotations

import math
from pathlib import Path

from .config import cfg
from .logger import log


def _humanise(seconds: float) -> str:
    """Render a duration as a compact human string."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def summarise(*, files: list, stats: dict, cached: int, chunks: list,
              required_by_file: dict, tolerance: int, suffix: str,
              source_lang: str, target_lang: str, mode: str,
              files_per_call=None) -> dict:
    """Build a summary of the work a batch would perform."""
    parallel = max(1, cfg.get("PARALLEL_CHUNKS", 1))
    cooldown = max(0, cfg.get("PARALLEL_COOLDOWN", 60) or 0)
    requests = len(chunks)
    batches = math.ceil(requests / parallel) if requests else 0

    from .blob import estimate_output_tokens
    tokens = sum(estimate_output_tokens(chunk) for chunk in chunks)

    return {
        "files": [Path(f).name for f in files],
        "cues": stats["total"],
        "unique": stats["unique"],
        "deduplicated": stats["collapsed"],
        "dedup_percent": stats["pct"],
        "cached": cached,
        "to_translate": sum(len(chunk) for chunk in chunks),
        "requests": requests,
        "batches": batches,
        "chunk_sizes": [len(chunk) for chunk in chunks],
        "estimated_output_tokens": tokens,
        # Only the pacing is predictable; API time is not, hence "at least".
        "minimum_seconds": max(0, batches - 1) * cooldown,
        "parallel_chunks": parallel,
        "cooldown": cooldown,
        "tolerance": tolerance,
        "suffix": suffix,
        "source_language": source_lang,
        "target_language": target_lang,
        "mode": mode,
        "files_per_call": files_per_call,
        "outputs": _expected_outputs(files, required_by_file, suffix),
    }


def _expected_outputs(files: list, required_by_file: dict, suffix: str) -> list:
    """Output names that would be produced, with the lines each needs."""
    from .sub_post import resolve_output_path

    outputs = []
    for index, source in enumerate(files, 1):
        needed = len(required_by_file.get(index, ()))
        outputs.append({
            "source": Path(source).name,
            "output": resolve_output_path(Path(source), suffix=suffix).name,
            "unique_lines": needed,
        })
    return outputs


def render(summary: dict) -> None:
    """Print one batch's dry-run summary."""
    log.sep()
    log.phase("DRY RUN - nothing will be sent or written")

    log.stat("Files", str(len(summary["files"])))
    log.stat("Cues", f"{summary['cues']} total")
    log.stat("Unique lines", f"{summary['unique']} "
                             f"({summary['deduplicated']} deduplicated, "
                             f"~{summary['dedup_percent']}% fewer tokens)")
    if summary["cached"]:
        log.stat("Already cached", f"{summary['cached']} line(s) - free")
    log.stat("To translate", f"{summary['to_translate']} line(s)")

    if summary["requests"]:
        log.stat("API requests", f"{summary['requests']} "
                                 f"in {summary['batches']} batch(es) "
                                 f"of {summary['parallel_chunks']}")
        log.stat("Chunk sizes", ", ".join(str(size) for size in summary["chunk_sizes"]))
        log.stat("Estimated output", f"~{summary['estimated_output_tokens']} tokens")
        log.stat("Minimum duration", f"{_humanise(summary['minimum_seconds'])} "
                                     f"of cooldown, plus API time")
    else:
        log.success("  No requests needed - everything is already cached")

    log.info("  Would write:")
    for entry in summary["outputs"]:
        log.item(f"{entry['output']}  ({entry['unique_lines']} unique line(s))")
    log.detail(f"  Partial tolerance: {summary['tolerance']} line(s)")


def render_total(summaries: list) -> None:
    """Print the combined total across every batch and folder."""
    if not summaries:
        return

    requests = sum(s["requests"] for s in summaries)
    tokens = sum(s["estimated_output_tokens"] for s in summaries)
    cached = sum(s["cached"] for s in summaries)
    to_translate = sum(s["to_translate"] for s in summaries)
    outputs = sum(len(s["outputs"]) for s in summaries)
    seconds = sum(s["minimum_seconds"] for s in summaries)

    log.sep()
    log.summary("Dry run total", [
        ("Output files", str(outputs)),
        ("Lines to translate", str(to_translate)),
        ("Lines from cache", str(cached)),
        ("API requests", str(requests)),
        ("Estimated output", f"~{tokens} tokens"),
        ("Minimum duration", f"{_humanise(seconds)} of cooldown, plus API time"),
    ])
    log.info("  Nothing was sent or written. Run again without --dry-run to start.")
