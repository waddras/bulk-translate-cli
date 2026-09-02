"""Per-directory btcli job manifests and extraction reuse.

Each media directory gets a hidden .btcli.json file. A translate command creates
one numbered job in every directory it touches. Jobs are append-only records;
previous extraction records may be reused when the extracted subtitle still
exists on disk.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .logger import log

MANIFEST_NAME = ".btcli.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _job_number(key: str) -> int:
    match = re.fullmatch(r"job(\d+)", key)
    return int(match.group(1)) if match else 0


def _load(directory: Path) -> dict:
    path = directory / MANIFEST_NAME
    if not path.exists():
        return {"series": "", "season": "", "jobs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("jobs", {}), dict):
            raise ValueError("manifest root or jobs field is not an object")
        data.setdefault("series", "")
        data.setdefault("season", "")
        data.setdefault("jobs", {})
        return data
    except Exception as exc:
        log.warning(f"Ignoring invalid {path}: {exc}")
        return {"series": "", "season": "", "jobs": {}}


def _save(directory: Path, data: dict) -> None:
    path = directory / MANIFEST_NAME
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _infer_season(files: list) -> str:
    seasons = set()
    for item in files:
        path = Path(item)
        match = re.search(r"(?i)(?:^|[^A-Z0-9])S(\d{1,2})(?:E\d+|[^A-Z0-9]|$)", path.stem)
        if match:
            seasons.add(f"S{int(match.group(1)):02d}")
    if len(seasons) == 1:
        return next(iter(seasons))
    if len(seasons) > 1:
        return "Multiple"

    directory_name = Path(files[0]).parent.name if files else ""
    match = re.search(r"(?i)season\s*(\d{1,2})|(?:^|\s)S(\d{1,2})(?:$|\s)", directory_name)
    if match:
        number = match.group(1) or match.group(2)
        return f"S{int(number):02d}"
    return directory_name


def find_reusable_extraction(video_path, tracks: list, suffix: str = "",
                             expected_extension: str | None = None) -> Path | None:
    """Find the newest matching extraction that exists and matches its source."""
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
        for entry in reversed(job.get("files", [])):
            if entry.get("video") != video.name:
                continue
            if [int(track) for track in entry.get("tracks", [])] != requested_tracks:
                continue
            if entry.get("source_size") != source_stat.st_size:
                continue
            if entry.get("source_mtime") != source_stat.st_mtime:
                continue
            extracted = entry.get("extracted")
            if not extracted:
                continue
            candidate = Path(extracted)
            if not candidate.is_absolute():
                candidate = video.parent / candidate
            if expected_name and candidate.name != expected_name:
                continue
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


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
    """One btcli translate run, represented once in every touched directory."""

    def __init__(self, command: dict):
        self.command = dict(command)
        self._states = {}

    def register_files(self, files: list, series: str = "") -> None:
        grouped = {}
        for item in files:
            path = Path(item)
            grouped.setdefault(path.parent.resolve(), []).append(path)
        for directory, directory_files in grouped.items():
            self._ensure(directory, directory_files, series)

    def _ensure(self, directory: Path, files: list | None = None, series: str = "") -> dict:
        directory = directory.resolve()
        if directory in self._states:
            state = self._states[directory]
            if series and not state["data"].get("series"):
                state["data"]["series"] = series
                _save(directory, state["data"])
            return state

        data = _load(directory)
        if series:
            data["series"] = series
        if files:
            data["season"] = _infer_season(files)
        jobs = data.setdefault("jobs", {})
        next_number = max((_job_number(key) for key in jobs), default=0) + 1
        job_key = f"job{next_number}"
        jobs[job_key] = {
            "type": "translate",
            "started_at": _now(),
            "finished_at": None,
            "status": "running",
            "command": dict(self.command),
            "files": [],
            "summary": {},
        }
        if files:
            field = "video" if self.command.get("input_type") == "vid" else "source"
            jobs[job_key]["files"] = [{field: Path(item).name} for item in files]
        state = {"data": data, "job_key": job_key}
        self._states[directory] = state
        _save(directory, data)
        return state

    def update_command(self, **values) -> None:
        self.command.update(values)
        for directory, state in self._states.items():
            state["data"]["jobs"][state["job_key"]]["command"].update(values)
            _save(directory, state["data"])

    def _entry(self, state: dict, *, video: str | None = None,
               source: str | None = None) -> dict:
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
        video = Path(video_path)
        extracted = Path(extracted_path)
        state = self._ensure(video.parent, [video])
        entry = self._entry(state, video=video.name)
        stat = video.stat()
        entry.update({
            "tracks": [int(track) for track in tracks],
            "extracted": extracted.name if extracted.parent == video.parent else str(extracted),
            "codec": codec,
            "reused_extraction": bool(reused),
            "source_size": stat.st_size,
            "source_mtime": stat.st_mtime,
            "extracted_at": _now(),
        })
        _save(video.parent.resolve(), state["data"])

    def record_translation(self, source_path, details: dict) -> None:
        source = Path(source_path)
        state = self._ensure(source.parent, [source])
        entry = self._entry(state, source=source.name)
        entry.setdefault("source", source.name)
        entry["translation"] = dict(details)
        entry["translation"]["recorded_at"] = _now()
        _save(source.parent.resolve(), state["data"])

    def finish(self) -> None:
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
