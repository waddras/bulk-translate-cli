"""Per-directory job records, and deciding when an extraction can be reused.

WHAT THIS FILE IS FOR
    Every media directory btcli touches gets a hidden ``.btcli.json``. It answers
    two questions later runs care about: what was done here before, and can the
    subtitles extracted last time be used again instead of running ffmpeg?

FILE SHAPE
    {
      "series": "Show Name",         # detected once, then left alone
      "season": "S03",               # inferred from filenames, else the folder
      "jobs": {
        "job1": {
          "type": "translate",
          "started_at": ..., "finished_at": ..., "status": ...,
          "command": {...},          # the settings this run used
          "files": [ {...}, ... ],   # one entry per video or subtitle file
          "summary": {...}
        },
        "job2": { ... }
      }
    }

HOW A RUN USES IT
    One run creates exactly one numbered job in *each* directory it touches, so a
    three-season translate leaves job records in three folders. ``ManifestRun``
    holds the open job for every directory and writes through on each update, so
    a run killed halfway still leaves a readable record rather than nothing.

    Jobs are append-only: nothing here is ever rewritten to look tidier. That is
    what makes the history worth reading, and why ``btcli prune`` exists to trim
    it deliberately rather than having writes quietly discard it.

WHY REUSE IS CONSERVATIVE
    Trusting a stale record silently translates the wrong subtitles, which is far
    worse than re-running ffmpeg. So ``find_reusable_extraction`` requires the
    manifest record *and* the extracted file to still agree with the video on
    disk. See its docstring for the full list of conditions.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .logger import log

MANIFEST_NAME = ".btcli.json"


def _now() -> str:
    """UTC timestamp as ``2026-06-28T14:02:11Z``.

    Whole seconds only, and a literal Z rather than ``+00:00``: these strings sit
    in a file people read, and they sort correctly as plain text.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _job_number(key: str) -> int:
    """The N in ``jobN``, or 0 for any key that is not a job.

    Used for ordering. Returning 0 for unrecognised keys means a hand-added or
    future field sorts below every real job instead of raising.
    """
    match = re.fullmatch(r"job(\d+)", key)
    return int(match.group(1)) if match else 0


def _load(directory: Path) -> dict:
    """Read the manifest, or return an empty one.

    Deliberately forgiving: a corrupt or hand-mangled manifest is reported and
    treated as absent, never fatal. Losing job history is an inconvenience;
    refusing to translate because of it would not be.
    """
    path = directory / MANIFEST_NAME
    if not path.exists():
        return {"series": "", "season": "", "jobs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("jobs", {}), dict):
            raise ValueError("manifest root or jobs field is not an object")
        # Fill in anything an older or partial manifest is missing, so callers
        # can index these keys without checking.
        data.setdefault("series", "")
        data.setdefault("season", "")
        data.setdefault("jobs", {})
        return data
    except Exception as exc:
        log.warning(f"Ignoring invalid {path}: {exc}")
        return {"series": "", "season": "", "jobs": {}}


def _save(directory: Path, data: dict) -> None:
    """Write the manifest atomically.

    Via a .tmp file and a rename, because this is written after every recorded
    step: a run interrupted mid-write would otherwise leave truncated JSON, and
    the next run would discard the whole history as invalid.
    """
    path = directory / MANIFEST_NAME
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _infer_season(files: list) -> str:
    """Best guess at which season these files belong to.

    Filenames win over the folder name, because they are the more reliable of the
    two: ``S03E01`` is unambiguous, whereas a folder may be called anything.
    Returns "Multiple" when files disagree, so a mixed batch is visibly mixed
    rather than silently labelled with whichever season happened to be first.
    Falls back to the raw folder name, which is still more use than "".
    """
    seasons = set()
    for item in files:
        path = Path(item)
        # SxxExx, or Sxx bounded by a non-alphanumeric, so "S03" is found but the
        # "s12" inside a word is not.
        match = re.search(r"(?i)(?:^|[^A-Z0-9])S(\d{1,2})(?:E\d+|[^A-Z0-9]|$)", path.stem)
        if match:
            seasons.add(f"S{int(match.group(1)):02d}")
    if len(seasons) == 1:
        return next(iter(seasons))
    if len(seasons) > 1:
        return "Multiple"

    # No episode numbering anywhere: fall back to "Season 3" / "S3" in the folder.
    directory_name = Path(files[0]).parent.name if files else ""
    match = re.search(r"(?i)season\s*(\d{1,2})|(?:^|\s)S(\d{1,2})(?:$|\s)", directory_name)
    if match:
        number = match.group(1) or match.group(2)
        return f"S{int(number):02d}"
    return directory_name


def find_reusable_extraction(video_path, tracks: list, suffix: str = "",
                             expected_extension: str | None = None) -> Path | None:
    """The newest earlier extraction that is still safe to use, or None.

    Every one of these must hold, and any failure moves on to the next candidate:

    * the record is for this video's filename
    * the track list matches exactly — extracting track 0 is not a substitute
      for tracks 0 and 2
    * the video's size *and* mtime are unchanged, so a re-encoded or replaced
      file is not paired with subtitles extracted from the old one
    * the extracted file still exists and is non-empty, so deleting it is a
      valid way to force a fresh extraction
    * its name matches the extension being asked for, so a request for .srt
      does not reuse an .ass

    Newest job first, so the most recent extraction wins.
    """
    video = Path(video_path)
    data = _load(video.parent)
    jobs = sorted(data.get("jobs", {}).items(), key=lambda item: _job_number(item[0]), reverse=True)
    requested_tracks = [int(track) for track in tracks]
    source_stat = video.stat()
    expected_name = (
        f"{video.stem}{suffix}.{expected_extension.lstrip('.')}"
        if expected_extension else None
    )

    for _, job in jobs:
        # Within a job, later entries are more recent too.
        for entry in reversed(job.get("files", [])):
            if entry.get("video") != video.name:
                continue
            # Compared as ints so "0" and 0 from older manifests both work.
            if [int(track) for track in entry.get("tracks", [])] != requested_tracks:
                continue
            if entry.get("source_size") != source_stat.st_size:
                continue
            if entry.get("source_mtime") != source_stat.st_mtime:
                continue
            extracted = entry.get("extracted")
            if not extracted:
                continue
            # Stored as a bare filename when it sits beside the video, so a moved
            # library still resolves; absolute paths are honoured as given.
            candidate = Path(extracted)
            if not candidate.is_absolute():
                candidate = video.parent / candidate
            if expected_name and candidate.name != expected_name:
                continue
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


def load_style_verdict(directory, styles: list) -> dict | None:
    """A previously agreed AI style verdict for this folder, if still valid.

    Kept per DIRECTORY rather than per series, because seasons of one show are
    routinely subtitled by different groups with different style names — a
    verdict for season 1 is not evidence about season 2.

    Reused only while the styles on disk still match the ones it was judged
    against, following the same rule as ``find_reusable_extraction``: a record is
    trusted only when the thing it describes has not changed underneath it. A
    re-release with renamed styles therefore gets a fresh verdict rather than a
    silently wrong one.
    """
    verdict = _load(Path(directory)).get("ai_verdict")
    if not isinstance(verdict, dict):
        return None
    if not isinstance(verdict.get("keep"), list) or not verdict["keep"]:
        return None
    if sorted(str(name) for name in verdict.get("styles", [])) != sorted(styles):
        log.detail("    Ignoring cached style verdict: the styles have changed")
        return None
    # Any style it chose must still exist, or the selection would be a no-op.
    if not set(verdict["keep"]).issubset(set(styles)):
        log.detail("    Ignoring cached style verdict: a chosen style is gone")
        return None
    return verdict


def save_style_verdict(directory, verdict: dict) -> None:
    """Record an agreed verdict for this folder.

    Written as its own top-level key, never into a job: jobs are append-only
    history, and this is current state that gets replaced.
    """
    if not verdict.get("keep"):
        # A whole-track verdict (a plain-text track with no styles) is not cached:
        # the reuse check is style-based, so there would be nothing to invalidate
        # it against. Re-asking costs one cheap call and keeps the invariant that
        # a cached verdict always names styles.
        return
    try:
        path = Path(directory)
        data = _load(path)
        data["ai_verdict"] = {
            "track": verdict.get("track"),
            "keep": list(verdict.get("keep", [])),
            "passthrough": list(verdict.get("passthrough", [])),
            "styles": list(verdict.get("styles", [])),
            "reason": verdict.get("reason", ""),
            "model": verdict.get("model", ""),
            "decided_at": _now(),
        }
        _save(path, data)
    except Exception as exc:
        # A verdict that cannot be cached is a lost optimisation, not a failure.
        log.detail(f"    Could not record style verdict: {exc}")


class NullManifestRun:
    """A manifest that records nothing.

    Used by a dry run, which must not create or modify .btcli.json. Accepting the
    same calls keeps the caller free of dry-run branches.
    """

    def register_files(self, files: list, series: str = "") -> None:
        pass

    def update_command(self, **values) -> None:
        pass

    def record_extraction(self, video_path, tracks: list, extracted_path,
                          codec: str, reused: bool) -> None:
        pass

    def record_translation(self, source_path, details: dict) -> None:
        pass

    def finish(self) -> None:
        pass


class ManifestRun:
    """One translate run, recorded as a numbered job in every directory it touches.

    Lifecycle: ``register_files`` opens a job per directory, ``update_command``
    records decisions made after the run began (detected show name, resolved
    styles), ``record_extraction`` and ``record_translation`` log per-file
    outcomes, and ``finish`` derives each job's final status.

    Every method writes to disk immediately rather than buffering until the end,
    so an interrupted run still leaves an accurate partial record. ``finish`` is
    called from a ``finally`` block for the same reason.
    """

    def __init__(self, command: dict):
        """Start a run. No manifest is touched until register_files is called.

        *command* is the settings this run used; it is recorded into each job so
        the record explains how its output was produced.
        """
        # Copied, because update_command mutates it as the run learns more and
        # the caller's dict should not change underneath them.
        self.command = dict(command)
        # directory -> {"data": manifest dict, "job_key": "jobN"}
        self._states = {}

    def register_files(self, files: list, series: str = "") -> None:
        """Open this run's job in each directory the given files live in.

        Files are grouped by parent directory, so one call covers a whole season
        or a whole series without the caller having to split them up.
        """
        grouped = {}
        for item in files:
            path = Path(item)
            grouped.setdefault(path.parent.resolve(), []).append(path)
        for directory, directory_files in grouped.items():
            self._ensure(directory, directory_files, series)

    def _ensure(self, directory: Path, files: list | None = None, series: str = "") -> dict:
        """Return this run's state for *directory*, creating its job on first use.

        Idempotent: called again for a directory already open, it returns the
        existing job rather than starting a second one — which is why recording
        an extraction and a translation for the same file lands in one job.
        """
        directory = directory.resolve()
        if directory in self._states:
            state = self._states[directory]
            # The series name is often only detected after the job was opened.
            # Fill it in if it was missing, but never overwrite a known value.
            if series and not state["data"].get("series"):
                state["data"]["series"] = series
                _save(directory, state["data"])
            return state

        data = _load(directory)
        if series:
            data["series"] = series
        if files:
            data["season"] = _infer_season(files)
        # Number from the highest existing job, so history is never overwritten
        # and gaps left by pruning do not cause a collision.
        jobs = data.setdefault("jobs", {})
        next_number = max((_job_number(key) for key in jobs), default=0) + 1
        job_key = f"job{next_number}"
        jobs[job_key] = {
            "type": "translate",
            "started_at": _now(),
            "finished_at": None,
            # Overwritten by finish(); left as "running" if the run is killed,
            # which is itself useful information.
            "status": "running",
            "command": dict(self.command),
            "files": [],
            "summary": {},
        }
        if files:
            # Video input is keyed by the video; subtitle input by the source
            # file. record_extraction and record_translation match on these.
            field = "video" if self.command.get("input_type") == "vid" else "source"
            jobs[job_key]["files"] = [{field: Path(item).name} for item in files]
        state = {"data": data, "job_key": job_key}
        self._states[directory] = state
        _save(directory, data)
        return state

    def update_command(self, **values) -> None:
        """Record decisions made after the job was opened, in every directory.

        Things like the detected show name and the resolved style lists are not
        known until the run is under way, but they belong with the command that
        produced the output.
        """
        self.command.update(values)
        for directory, state in self._states.items():
            state["data"]["jobs"][state["job_key"]]["command"].update(values)
            _save(directory, state["data"])

    def _entry(self, state: dict, *, video: str | None = None,
               source: str | None = None) -> dict:
        """Find or create the per-file record inside this run's job.

        A subtitle extracted from a video is recorded against the video entry,
        so ``source`` also matches an entry's ``extracted`` name — otherwise
        translating an extracted file would start a second, unlinked entry for
        what is really the same file.
        """
        job = state["data"]["jobs"][state["job_key"]]
        for entry in job["files"]:
            if video and entry.get("video") == video:
                return entry
            if source and source in (entry.get("source"), entry.get("extracted")):
                return entry
        entry = {}
        if video:
            entry["video"] = video
        if source:
            entry["source"] = source
        job["files"].append(entry)
        return entry

    def record_extraction(self, video_path, tracks: list, extracted_path,
                          codec: str, reused: bool) -> None:
        """Log what was pulled out of a video, and the fingerprint to check later.

        The video's size and mtime are stored precisely so
        ``find_reusable_extraction`` can tell whether the file has changed since.
        Without them, a replaced video would silently reuse the old subtitles.
        """
        video = Path(video_path)
        extracted = Path(extracted_path)
        state = self._ensure(video.parent, [video])
        entry = self._entry(state, video=video.name)
        stat = video.stat()
        entry.update({
            "tracks": [int(track) for track in tracks],
            # Bare name when it sits beside the video, so a moved library still
            # resolves; a full path only when it does not.
            "extracted": extracted.name if extracted.parent == video.parent else str(extracted),
            "codec": codec,
            "reused_extraction": bool(reused),
            "source_size": stat.st_size,
            "source_mtime": stat.st_mtime,
            "extracted_at": _now(),
        })
        _save(video.parent.resolve(), state["data"])

    def record_translation(self, source_path, details: dict) -> None:
        """Log the outcome of translating one file.

        *details* carries the status finish() reads to decide the job's overall
        result, plus the model, mode and any untranslated lines.
        """
        source = Path(source_path)
        state = self._ensure(source.parent, [source])
        entry = self._entry(state, source=source.name)
        # An entry created for a video has no source field yet.
        entry.setdefault("source", source.name)
        entry["translation"] = dict(details)
        entry["translation"]["recorded_at"] = _now()
        _save(source.parent.resolve(), state["data"])

    def finish(self) -> None:
        """Close this run's job in every directory and derive its status.

        Status is deduced from the per-file translation records:

        * every file complete            -> "complete"
        * some complete, some not        -> "partial"
        * files attempted, none complete -> "failed"
        * nothing translated, but video subtitles were extracted -> "extracted"
        * nothing at all                 -> "failed"

        "extracted" matters because a run that pulled subtitles out and then hit
        a quota wall did real, reusable work; calling that a failure would be
        both wrong and discouraging.

        Always called from a ``finally`` block, so a job is closed even when the
        run raised.
        """
        for directory, state in self._states.items():
            job = state["data"]["jobs"][state["job_key"]]
            translations = [entry.get("translation") for entry in job["files"] if entry.get("translation")]
            statuses = [item.get("status") for item in translations]
            if statuses and all(status == "complete" for status in statuses):
                status = "complete"
            elif any(status == "complete" for status in statuses):
                status = "partial"
            elif statuses:
                status = "failed"
            elif job.get("command", {}).get("input_type") == "vid" and any(
                entry.get("extracted") for entry in job["files"]
            ):
                status = "extracted"
            else:
                status = "failed"
            job["status"] = status
            job["finished_at"] = _now()
            job["summary"] = {
                "files": len(job["files"]),
                "translations_complete": statuses.count("complete"),
                "translations_incomplete": len(statuses) - statuses.count("complete"),
            }
            _save(directory, state["data"])
