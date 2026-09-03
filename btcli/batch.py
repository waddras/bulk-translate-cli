"""Per-file output emission for one batch of subtitle files.

Extracted from a 307-line function whose five nested closures shared a dozen
captured variables, which made threading new options through it error-prone.
Holding that state on an object instead lets the emission rules be tested
directly, without running a translation.

The central rule: a file is written only when every unique line it needs has a
translation. Files short a few lines are written once the tolerance allows it,
with those lines left in the source language.
"""
from __future__ import annotations

from pathlib import Path

from .blob import expand_translations
from .config import cfg
from .logger import log
from .sub_post import reassemble_files

# Untranslated lines recorded per file in the manifest, so a job record stays a
# reasonable size on a batch where a whole chunk failed.
MAX_RECORDED_LINES = 50


class BatchWriter:
    """Decides when each output file may be written, and writes it.

    Usage during a batch:

    * ``write_ready`` after every API response — emits any file now complete
    * ``finalize`` once at the end — writes what the tolerance permits and
      reports the rest
    * ``completed``, ``warnings`` and ``emitted`` carry the outcome

    Files are emitted as soon as they are ready rather than at the end, so a run
    interrupted halfway leaves finished episodes on disk instead of nothing.
    """

    def __init__(self, files: list, meta: dict, payload: dict, chunks: list, *,
                 suffix: str, force_srt: bool, keep_styles, passthrough_styles,
                 source_lang: str, target_lang: str, mode: str,
                 files_per_call, manifest_run=None):
        """Precompute what each file needs, so readiness is a set comparison.

        The indexes built here (``cues_by_file``, ``required_by_file``,
        ``_chunk_keys``) are what make ``write_ready`` cheap enough to call after
        every single response.

        File indexes are 1-based throughout, matching the FF part of a blob tag.
        """
        self.files = files
        self.meta = meta
        self.payload = payload
        self.suffix = suffix
        self.force_srt = force_srt
        self.keep_styles = keep_styles
        self.passthrough_styles = passthrough_styles
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.mode = mode
        self.files_per_call = files_per_call
        self.manifest_run = manifest_run

        # Which cues belong to each file, and the unique lines each file needs.
        # "Required" is the set of *representative* tags, not cue tags: a file
        # with fifty "Yes." cues needs that line translated once.
        self.cues_by_file = {index: [] for index in range(1, len(files) + 1)}
        for tag, item in meta.items():
            self.cues_by_file[item["file_idx"]].append((tag, item))
        self.required_by_file = {
            index: {item["rep"] for _, item in cues}
            for index, cues in self.cues_by_file.items()
        }

        # Kept for the manifest record, which reports how a file's lines were
        # distributed across requests — useful when diagnosing which chunk failed.
        self.chunk_sizes = [len(chunk) for chunk in chunks]
        self._chunk_keys = [set(chunk) for chunk in chunks]

        self.emitted: set = set()
        self.completed: list = []
        self.warnings: list = []

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _source(self, file_idx: int) -> Path:
        """The source file for a 1-based blob file index."""
        return Path(self.files[file_idx - 1])

    def _recorded_lines(self, missing_keys) -> list:
        """Untranslated source lines to store in the manifest, capped and ordered.

        Sorted for a stable record, and truncated so a batch where a whole chunk
        failed cannot bloat .btcli.json with thousands of lines.
        """
        return [self.payload[key] for key in sorted(missing_keys)
                if key in self.payload][:MAX_RECORDED_LINES]

    def details(self, file_idx: int, translated: dict, status: str,
                output: str | None = None) -> dict:
        """Manifest record for one file."""
        cues = self.cues_by_file[file_idx]
        required = self.required_by_file[file_idx]
        translated_cues = sum(1 for _, item in cues if item["rep"] in translated)
        per_chunk = [len(required & keys) for keys in self._chunk_keys]
        return {
            "output": output,
            "source_language": self.source_lang,
            "target_language": self.target_lang,
            "suffix": self.suffix,
            "model": cfg.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            "model_pool": cfg.get("MODEL_POOL", []),
            "mode": self.mode,
            "files_per_call": self.files_per_call,
            "max_lines_per_chunk": (None if self.files_per_call
                                    else cfg.get("MAX_LINES_PER_CHUNK", 1000)),
            "batch_chunk_sizes": self.chunk_sizes,
            "file_lines_per_chunk": [size for size in per_chunk if size],
            "cues": len(cues),
            "unique_lines": len(required),
            "deduplicated_lines": len(cues) - len(required),
            "translated": translated_cues,
            "total": len(cues),
            "missing_unique": len(required - set(translated)),
            "styles_to_translate": self.keep_styles or [],
            "passthrough_styles": self.passthrough_styles or [],
            "status": status,
            "elapsed": log.elapsed(),
        }

    # ── Writing ───────────────────────────────────────────────────────────────

    def emit(self, file_idx: int, translated: dict, status: str,
             missing_keys=None) -> bool:
        """Write one output file and record it. Returns True when written."""
        source = self._source(file_idx)
        try:
            translated_blob = expand_translations(translated, self.meta)
            written, file_warnings = reassemble_files(
                translated_blob, self.meta, self.files,
                suffix=self.suffix, force_srt=self.force_srt,
                kept_styles=self.keep_styles,
                passthrough_styles=self.passthrough_styles,
                only_file_indices={file_idx},
            )
        except Exception as exc:
            log.detail(f"    Could not write {source.name}: {exc}")
            return False

        if not written:
            return False

        self.emitted.add(file_idx)
        self.completed.extend(written)
        self.warnings.extend(file_warnings)

        if self.manifest_run:
            record = self.details(file_idx, translated, status, written[0])
            if missing_keys:
                record["untranslated_lines"] = self._recorded_lines(missing_keys)
            self.manifest_run.record_translation(source, record)
        return True

    def write_ready(self, translated: dict) -> None:
        """Write every file whose lines are now all translated.

        Called after each API response, so a completed file lands immediately
        rather than waiting for the rest of the batch.
        """
        available = set(translated)
        for file_idx in range(1, len(self.files) + 1):
            if file_idx in self.emitted:
                continue
            required = self.required_by_file[file_idx]
            if not required or not required.issubset(available):
                continue
            log.info(f"  All lines ready: {self._source(file_idx).name} "
                     f"— generating output now")
            self.emit(file_idx, translated, "complete")

    def finalize(self, translated: dict, tolerance: int) -> dict:
        """Write or report whatever is left. Returns the still-missing lines.

        The result maps a representative key to its source text and the outputs
        that still need it, so the lines can be listed for review rather than
        only counted.
        """
        self.write_ready(translated)
        still_missing: dict = {}

        for file_idx in range(1, len(self.files) + 1):
            if file_idx in self.emitted:
                continue

            source = self._source(file_idx)
            missing_keys = self.required_by_file[file_idx] - set(translated)
            for key in missing_keys:
                entry = still_missing.setdefault(
                    key, {"text": self.payload.get(key, ""), "files": []})
                if source.name not in entry["files"]:
                    entry["files"].append(source.name)

            if missing_keys and len(missing_keys) <= tolerance:
                log.info(f"  {source.name}: {len(missing_keys)} line(s) missing "
                         f"(within tolerance of {tolerance}) — writing anyway")
                for key in sorted(missing_keys):
                    log.detail(f"      untranslated: {self.payload.get(key, '')[:80]}")
                if self.emit(file_idx, translated, "partial", missing_keys):
                    message = (f"{source.name}: written with {len(missing_keys)} "
                               f"line(s) left in {self.source_lang}")
                    log.warning(message)
                    self.warnings.append(message)
                    continue

            message = (f"{source.name}: not written because {len(missing_keys)} "
                       f"unique line(s) remain untranslated")
            log.warning(message)
            self.warnings.append(message)
            if self.manifest_run:
                record = self.details(file_idx, translated, "incomplete")
                record["untranslated_lines"] = self._recorded_lines(missing_keys)
                self.manifest_run.record_translation(source, record)

        return still_missing
