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

HOW A JOB USES IT
    1. ``split()`` divides the payload into lines already known and lines that
       still need sending. Only the latter reach the API.
    2. ``store_and_flush()`` is called after every response, so each batch of
       translations is durable before the next request goes out.
    3. ``loaded`` and ``added`` drive the reporting, so it is always visible
       whether resuming was in effect.
    4. ``from_earlier_run()`` answers "was this line here before I started?",
       which is what the resume prompt is really asking about.

FIRST-OPEN SNAPSHOT
    A run opens this file many times — once per batch of files, and once per
    folder in interactive mode — and each open re-reads a file the run itself
    has been growing. So "the cache contains this line" is NOT the same question
    as "this line came from an earlier run", and only the second one is worth
    prompting a human about.

    ``_snapshots`` records the key set each cache file held the FIRST time this
    process opened it, and is never refreshed. Without it, season 1 fills the
    shared cache and season 2 mistakes its own run's work for an earlier run's,
    which is what used to make the resume prompt fire mid-run.

FILE SHAPE
    {
      "version": 1,
      "series": "Fruits Basket (2019)",
      "languages": {
        "arabic": {"<20-hex content key>": "translated text", ...},
        "french": {...}
      },
      "updated": "2026-06-28T14:02:11Z"
    }

    Languages are separate sections, so translating a series into Arabic never
    disturbs the French entries — see ``flush``, which merges rather than
    overwrites for exactly this reason.
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

# (resolved cache path, language) → keys held the first time this process
# opened that file. See FIRST-OPEN SNAPSHOT in the module docstring.
_snapshots: dict = {}


def reset_snapshots() -> None:
    """Forget every first-open snapshot, so the next open is treated as the first.

    Only meaningful between tests: within a real run the snapshots must survive
    for the whole process, which is the entire point of them.
    """
    _snapshots.clear()


def _register_snapshot(path: Path, target_lang: str, entries: dict) -> frozenset:
    """Return the first-open key set for this cache file, registering it if new.

    Deliberately does not refresh an existing entry: a second TranslationCache
    for the same file is reading a file this run has already added to, and
    treating those additions as pre-existing is the bug this guards against.
    """
    key = (str(Path(path).resolve()), target_lang)
    if key not in _snapshots:
        _snapshots[key] = frozenset(entries)
    return _snapshots[key]


def _now() -> str:
    """UTC timestamp as ``2026-06-28T14:02:11Z``, for the file's updated field."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _key(text: str) -> str:
    """Stable content key for a source line.

    Whitespace is collapsed first, so a line that differs only in indentation or
    line wrapping still hits the same entry — the translation would be identical.

    Truncated to 20 hex characters (80 bits). Long enough that a collision across
    a library of subtitles is not a practical concern, short enough that the
    cache file stays readable.
    """
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
        """Open (and immediately read) the cache for one series and language.

        Constructing this is cheap and side-effect-free on disk: nothing is
        written until something is stored.
        """
        self.root = Path(root)
        # Lower-cased so "Arabic" and "arabic" share one section.
        self.target_lang = target_lang.lower()
        self.path = self.root / CACHE_NAME
        self._entries: dict = {}
        self._dirty = False
        # Kept separate from len(self._entries) so reporting can distinguish
        # "found on disk" from "learned during this job".
        self._loaded_count = 0
        self._added = 0
        self._load()
        # Registered after loading and only once per process, so this stays the
        # state before the run began even on the tenth open of the same file.
        self._preexisting = _register_snapshot(self.path, self.target_lang,
                                               self._entries)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Read this language's entries. An unreadable cache is skipped, not fatal.

        Losing cached lines costs quota; refusing to translate because of a
        damaged cache would cost the whole job.
        """
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
        """Write the cache to disk, preserving other languages' entries.

        The file is re-read here rather than trusted from construction time,
        because only this language's section is ours to replace. Writing the
        in-memory state wholesale would delete every other language.

        Written atomically via a .tmp rename: this is called after every API
        response, so a partial write is a real possibility.
        """
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

    def from_earlier_run(self, payload: dict) -> set:
        """Tags in {tag: source} whose translation predates this process.

        This is the set the resume prompt may speak for. Anything outside it was
        translated by the run that is happening right now, so describing it as
        "from an earlier run" would be false and asking about it would mean
        interrupting a run to ask permission to reuse its own work.
        """
        return {tag for tag, source in payload.items()
                if _key(source) in self._preexisting}

    def store(self, payload: dict, translated: dict) -> int:
        """Record translations, keyed by their source text. Returns new entries.

        *payload* supplies the source text each tag came from — the cache is
        keyed by content, so a translation cannot be stored without it.

        Silently skips anything unusable: a tag with no matching source, a
        non-string, or blank text. Caching an empty translation would poison
        future runs, which would then skip the line believing it done.
        """
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
        """Entries that were already on disk when this cache object was opened."""
        return self._loaded_count

    @property
    def preexisting(self) -> frozenset:
        """Keys on disk when this process FIRST opened this cache file.

        Differs from ``loaded`` from the second batch onwards: ``loaded`` counts
        what this object read, which includes lines the current run has since
        contributed.
        """
        return self._preexisting

    @property
    def added(self) -> int:
        """Entries recorded during this job."""
        return self._added

    def __len__(self) -> int:
        """Total entries held for this language."""
        return len(self._entries)

    def __bool__(self) -> bool:
        # A cache object is always usable; without this an EMPTY cache would be
        # falsy through __len__ and callers would silently skip storing lines.
        return True
