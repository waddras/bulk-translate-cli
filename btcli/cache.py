"""Per-series translation cache, so no line is ever paid for twice.

The cache is a notebook of every line already translated. Entries are written
during the job, as each API response arrives, not at the end. If a run is
interrupted, killed, or ends with files incomplete, the translated lines are
already on disk and the next run only sends what is still missing.

Keys are a hash of the SOURCE TEXT, never a position, so renaming files,
reordering cues, or re-extracting a track can never mis-attribute a
translation. Entries are scoped per target language.

Location: one file per series, in the series root, e.g.
    Fruits Basket (2019)/.btcli-cache.json
A path pointing at a season folder resolves to the same file, so seasons of one
series share their cached lines.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .logger import log

CACHE_NAME = ".btcli-cache.json"
CACHE_VERSION = 1

# Folder names that are a season of a series rather than the series itself.
_SEASON_RE = re.compile(r"^(?:season|series|saison|s)[\s._-]*\d+$|^\d{1,2}$", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _key(text: str) -> str:
    """Stable content key for a source line."""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def series_root_for(files) -> Path:
    """Directory that should hold the cache for these files.

    A single season folder resolves to its parent so all seasons of a series
    share one cache; anything else uses the common ancestor of the files.
    """
    parents = {Path(f).resolve().parent for f in files}
    if not parents:
        return Path.cwd()

    if len(parents) == 1:
        directory = next(iter(parents))
        if _SEASON_RE.match(directory.name) and directory.parent != directory:
            return directory.parent
        return directory

    common = Path(os.path.commonpath([str(p) for p in parents]))
    return common


class TranslationCache:
    """Content-keyed store of translated lines for one series and language."""

    def __init__(self, root: Path, target_lang: str):
        self.root = Path(root)
        self.target_lang = target_lang.lower()
        self.path = self.root / CACHE_NAME
        self._entries: dict = {}
        self._dirty = False
        self._loaded_count = 0
        self._added = 0
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            languages = data.get("languages", {})
            entries = languages.get(self.target_lang, {})
            if not isinstance(entries, dict):
                raise ValueError("language section is not an object")
            self._entries = {k: v for k, v in entries.items() if isinstance(v, str)}
            self._loaded_count = len(self._entries)
        except Exception as exc:
            log.warning(f"Ignoring unreadable cache {self.path}: {exc}")
            self._entries = {}

    def flush(self, force: bool = False) -> None:
        """Write the cache to disk, preserving other languages' entries."""
        if not self._dirty and not force:
            return

        payload = {"version": CACHE_VERSION, "languages": {}}
        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("languages"), dict):
                    payload["languages"] = existing["languages"]
                    payload["series"] = existing.get("series", self.root.name)
            except Exception:
                pass  # unreadable file is replaced rather than blocking the job

        payload.setdefault("series", self.root.name)
        payload["languages"][self.target_lang] = self._entries
        payload["updated"] = _now()

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                                 encoding="utf-8")
            temporary.replace(self.path)
            self._dirty = False
        except Exception as exc:
            log.warning(f"Could not write cache {self.path}: {exc}")

    # ── Lookup and storage ────────────────────────────────────────────────────

    def split(self, payload: dict) -> tuple:
        """Split {tag: source} into (cached {tag: translated}, missing {tag: source})."""
        cached, missing = {}, {}
        for tag, source in payload.items():
            hit = self._entries.get(_key(source))
            if hit is None:
                missing[tag] = source
            else:
                cached[tag] = hit
        return cached, missing

    def store(self, payload: dict, translated: dict) -> int:
        """Record translations, keyed by their source text. Returns new entries."""
        added = 0
        for tag, text in translated.items():
            source = payload.get(tag)
            if source is None or not isinstance(text, str) or not text.strip():
                continue
            key = _key(source)
            if self._entries.get(key) == text:
                continue
            self._entries[key] = text
            added += 1
        if added:
            self._added += added
            self._dirty = True
        return added

    def store_and_flush(self, payload: dict, translated: dict) -> int:
        """Record translations and persist immediately (called per API response)."""
        added = self.store(payload, translated)
        if added:
            self.flush()
        return added

    @property
    def entries(self) -> dict:
        """The stored content-key to translation mapping, read-only by convention."""
        return self._entries

    def remove_keys(self, keys) -> int:
        """Drop entries by content key. Used by pruning."""
        removed = 0
        for key in list(keys):
            if self._entries.pop(key, None) is not None:
                removed += 1
        if removed:
            self._dirty = True
        return removed

    def forget(self, payload: dict) -> int:
        """Drop cached entries for these sources (used by --no-cache refresh)."""
        removed = 0
        for source in payload.values():
            if self._entries.pop(_key(source), None) is not None:
                removed += 1
        if removed:
            self._dirty = True
        return removed

    # ── Reporting ─────────────────────────────────────────────────────────────

    @property
    def loaded(self) -> int:
        """Entries that were already on disk when the job started."""
        return self._loaded_count

    @property
    def added(self) -> int:
        """Entries recorded during this job."""
        return self._added

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        # A cache object is always usable; without this an EMPTY cache would be
        # falsy through __len__ and callers would silently skip storing lines.
        return True
