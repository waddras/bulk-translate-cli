"""Housekeeping for the state files btcli leaves beside your media.

Both files grow without bound. The cache accumulates every line ever translated
for a series, and the manifest accumulates every job ever run in a directory.
Neither is harmful for a while, but there was no way to inspect or trim them.

Two independent jobs, because they answer different questions:
  - the cache is only worth trimming when entries no longer match any source
  - the manifest is a history, so trimming means keeping the most recent jobs

Reporting is the default. Nothing is deleted without --apply.
"""
from __future__ import annotations

from pathlib import Path

from .cache import CACHE_NAME, TranslationCache, _key
from .config import cfg
from .logger import log
from .manifest import MANIFEST_NAME, _job_number, _load, _save

# Directories that never hold media, skipped while searching for state files.
_SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _size(path: Path) -> int:
    """File size, or 0 if it cannot be read.

    Pruning reports sizes across many folders; a permission error on one should
    not abort the report.
    """
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _kilobytes(size: int) -> str:
    """Size as KB. State files are small; KB throughout is easier to compare."""
    return f"{size / 1024:.1f} KB"


def find_state_files(root: Path, name: str) -> list:
    """Every state file of one kind under a path, at any depth."""
    if root.is_file():
        return [root] if root.name == name else []
    found = []
    for path in root.rglob(name):
        if any(part in _SKIP for part in path.parts):
            continue
        found.append(path)
    return sorted(found)


# ── Cache ─────────────────────────────────────────────────────────────────────

def _live_source_keys(directory: Path, target_lang: str) -> set:
    """Content keys for every line currently present in the source subtitles.

    A cache entry not in this set corresponds to no line in any subtitle nearby,
    so it can never be reused from here.
    """
    from .srt_pre import parse_subtitle_file

    extensions = set(cfg.get("SOURCE_EXTENSIONS", [".srt", ".ass", ".ssa"]))
    codes = cfg.get("LANGUAGE_CODES", {})
    output_marker = f".{codes.get(target_lang.lower(), target_lang[:2].lower())}."

    keys = set()
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if output_marker in path.name.lower():
            continue                      # this tool's own output, not a source
        if any(part in _SKIP for part in path.parts):
            continue
        try:
            for cue in parse_subtitle_file(path):
                keys.add(_key(cue["text"]))
        except Exception as exc:
            log.detail(f"    Could not read {path.name}: {exc}")
    return keys


def prune_cache(root: Path, target_lang: str, apply: bool = False) -> dict:
    """Report, and optionally remove, cache entries no longer matching a source."""
    caches = find_state_files(root, CACHE_NAME)
    if not caches:
        log.info(f"No {CACHE_NAME} found under {root}")
        return {"files": 0, "removed": 0, "kept": 0, "bytes_saved": 0}

    total_removed = total_kept = bytes_saved = 0

    for path in caches:
        directory = path.parent
        cache = TranslationCache(directory, target_lang)
        if not len(cache):
            log.detail(f"  {path}: no entries for {target_lang}")
            continue

        live = _live_source_keys(directory, target_lang)
        stale = [key for key in cache.entries if key not in live]

        before = _size(path)
        log.info(f"  {directory.name or directory}: {len(cache)} entry(ies), "
                 f"{len(stale)} no longer match a source line")

        if not stale:
            total_kept += len(cache)
            continue

        if apply:
            cache.remove_keys(stale)
            cache.flush()
            saved = before - _size(path)
            bytes_saved += max(0, saved)
            log.success(f"    Removed {len(stale)} entry(ies), {_kilobytes(max(0, saved))} freed")
        else:
            log.info(f"    Would remove {len(stale)} entry(ies)")

        total_removed += len(stale)
        total_kept += len(cache) - len(stale)

    return {"files": len(caches), "removed": total_removed,
            "kept": total_kept, "bytes_saved": bytes_saved}


# ── Manifest ──────────────────────────────────────────────────────────────────

def prune_manifests(root: Path, keep: int, apply: bool = False) -> dict:
    """Report, and optionally trim, job history to the most recent jobs."""
    manifests = find_state_files(root, MANIFEST_NAME)
    if not manifests:
        log.info(f"No {MANIFEST_NAME} found under {root}")
        return {"files": 0, "removed": 0, "kept": 0, "bytes_saved": 0}

    total_removed = total_kept = bytes_saved = 0

    for path in manifests:
        directory = path.parent
        data = _load(directory)
        jobs = data.get("jobs", {})
        if not jobs:
            continue

        ordered = sorted(jobs, key=_job_number)
        surplus = ordered[:-keep] if keep > 0 else ordered
        before = _size(path)

        log.info(f"  {directory.name or directory}: {len(jobs)} job(s), "
                 f"{len(surplus)} older than the last {keep}")

        if not surplus:
            total_kept += len(jobs)
            continue

        if apply:
            for job in surplus:
                jobs.pop(job, None)
            # Renumber so the history stays job1..jobN rather than gaining holes.
            remaining = sorted(jobs.items(), key=lambda item: _job_number(item[0]))
            data["jobs"] = {f"job{index}": job
                            for index, (_, job) in enumerate(remaining, 1)}
            _save(directory, data)
            saved = before - _size(path)
            bytes_saved += max(0, saved)
            log.success(f"    Removed {len(surplus)} job(s), "
                        f"{_kilobytes(max(0, saved))} freed")
        else:
            log.info(f"    Would remove {len(surplus)} job(s): "
                     f"{', '.join(surplus[:5])}"
                     f"{' ...' if len(surplus) > 5 else ''}")

        total_removed += len(surplus)
        total_kept += len(jobs) if apply else len(jobs) - len(surplus)

    return {"files": len(manifests), "removed": total_removed,
            "kept": total_kept, "bytes_saved": bytes_saved}


# ── Reporting ─────────────────────────────────────────────────────────────────

def show_usage(root: Path) -> dict:
    """Report how much space the state files occupy."""
    caches = find_state_files(root, CACHE_NAME)
    manifests = find_state_files(root, MANIFEST_NAME)
    cache_bytes = sum(_size(p) for p in caches)
    manifest_bytes = sum(_size(p) for p in manifests)

    log.sep()
    log.phase("STATE FILES")
    log.stat("Translation caches", f"{len(caches)} file(s), {_kilobytes(cache_bytes)}")
    log.stat("Job manifests", f"{len(manifests)} file(s), {_kilobytes(manifest_bytes)}")
    for path in caches + manifests:
        log.detail(f"  {_kilobytes(_size(path)):>10}  {path}")
    return {"caches": len(caches), "manifests": len(manifests),
            "bytes": cache_bytes + manifest_bytes}


def run_prune(path: str, what: str = "all", keep: int = 10,
              apply: bool = False, target_lang: str | None = None) -> None:
    """Entry point for the prune command."""
    root = Path(path).expanduser()
    if not root.exists():
        log.error(f"Path does not exist: {root}")
        return

    target_lang = target_lang or cfg.get("TARGET_LANGUAGE", "arabic")
    wanted = {item.strip() for item in what.split(",") if item.strip()}
    if "all" in wanted:
        wanted = {"cache", "manifest"}

    unknown = wanted - {"cache", "manifest"}
    if unknown:
        log.error(f"Unknown target(s): {', '.join(sorted(unknown))}. "
                  f"Use cache, manifest, or all.")
        return

    show_usage(root)

    if not apply:
        log.warning("Reporting only. Add --apply to make these changes.")

    results = {}
    if "cache" in wanted:
        log.sep()
        log.phase(f"CACHE - entries with no matching source line ({target_lang})")
        results["cache"] = prune_cache(root, target_lang, apply=apply)

    if "manifest" in wanted:
        log.sep()
        log.phase(f"MANIFEST - job history beyond the last {keep}")
        results["manifest"] = prune_manifests(root, keep=keep, apply=apply)

    log.sep()
    rows = []
    for name, result in results.items():
        rows.append((f"{name} entries removed" if apply else f"{name} removable",
                     str(result["removed"])))
        if apply and result["bytes_saved"]:
            rows.append((f"{name} space freed", _kilobytes(result["bytes_saved"])))
    log.summary("Prune complete" if apply else "Prune preview", rows or [("Nothing to do", "0")])

    if not apply and any(r["removed"] for r in results.values()):
        log.info("  Re-run with --apply to remove them.")
