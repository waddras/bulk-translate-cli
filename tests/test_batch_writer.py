"""Per-file emission rules.

These were previously locked inside nested closures in a 307-line function and
could only be reached by running a whole translation. Testing them directly is
the reason that logic was extracted.

The rule under test: a file is written only when every unique line it needs has
a translation, with a tolerance for files short just a few lines.
"""
from __future__ import annotations

import pysubs2

from btcli.batch import BatchWriter
from btcli.blob import build_blob


def _writer(files, chunks=None, manifest_run=None, **overrides):
    meta, payload, _ = build_blob(files)
    settings = dict(
        suffix=".ar", force_srt=False, keep_styles=None, passthrough_styles=None,
        source_lang="english", target_lang="arabic", mode="chunked",
        files_per_call=None, manifest_run=manifest_run,
    )
    settings.update(overrides)
    writer = BatchWriter(files, meta, payload,
                         chunks if chunks is not None else [payload], **settings)
    return writer, payload


def _translate_all(payload):
    return {key: f"AR[{text}]" for key, text in payload.items()}


# ── Requirements ──────────────────────────────────────────────────────────────

def test_each_file_knows_the_unique_lines_it_needs(tmp_path, ass_factory):
    first = ass_factory(tmp_path / "a.en.ass",
                        [("Default", "shared"), ("Default", "only a")])
    second = ass_factory(tmp_path / "b.en.ass", [("Default", "shared")])
    writer, payload = _writer([first, second])

    assert len(writer.required_by_file[1]) == 2
    assert len(writer.required_by_file[2]) == 1
    # "shared" is deduplicated, so both files depend on the same key.
    assert writer.required_by_file[2] <= writer.required_by_file[1]


# ── write_ready ───────────────────────────────────────────────────────────────

def test_a_file_is_written_once_all_its_lines_arrive(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass",
                         [("Default", "one"), ("Default", "two")])
    writer, payload = _writer([source])

    writer.write_ready(_translate_all(payload))
    assert writer.completed == ["a.ar.ass"]
    assert (tmp_path / "a.ar.ass").exists()


def test_a_file_is_not_written_while_a_line_is_missing(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass",
                         [("Default", "one"), ("Default", "two")])
    writer, payload = _writer([source])

    partial = _translate_all(payload)
    partial.pop(next(iter(partial)))
    writer.write_ready(partial)

    assert writer.completed == []
    assert not (tmp_path / "a.ar.ass").exists()


def test_a_completed_file_is_written_only_once(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass", [("Default", "one")])
    writer, payload = _writer([source])

    translated = _translate_all(payload)
    writer.write_ready(translated)
    writer.write_ready(translated)
    assert writer.completed == ["a.ar.ass"], "must not be emitted twice"


def test_one_ready_file_is_written_while_another_waits(tmp_path, ass_factory):
    ready = ass_factory(tmp_path / "ready.en.ass", [("Default", "done")])
    waiting = ass_factory(tmp_path / "waiting.en.ass", [("Default", "pending")])
    writer, payload = _writer([ready, waiting])

    only_ready = {key: f"AR[{text}]" for key, text in payload.items()
                  if text == "done"}
    writer.write_ready(only_ready)

    assert writer.completed == ["ready.ar.ass"]
    assert not (tmp_path / "waiting.ar.ass").exists()


# ── finalize and tolerance ────────────────────────────────────────────────────

def test_finalize_writes_a_file_within_tolerance(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass",
                         [("Default", "good"), ("Default", "bad")])
    writer, payload = _writer([source])

    translated = {key: f"AR[{text}]" for key, text in payload.items()
                  if text != "bad"}
    missing = writer.finalize(translated, tolerance=1)

    assert (tmp_path / "a.ar.ass").exists()
    assert len(missing) == 1
    texts = [event.text for event in pysubs2.SSAFile.load(str(tmp_path / "a.ar.ass"))]
    assert any("bad" in text and "AR[" not in text for text in texts)


def test_finalize_skips_a_file_beyond_tolerance(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass",
                         [("Default", "bad one"), ("Default", "bad two")])
    writer, payload = _writer([source])

    missing = writer.finalize({}, tolerance=1)
    assert not (tmp_path / "a.ar.ass").exists()
    assert len(missing) == 2
    assert any("not written" in w for w in writer.warnings)


def test_zero_tolerance_means_all_or_nothing(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass",
                         [("Default", "good"), ("Default", "bad")])
    writer, payload = _writer([source])
    translated = {key: f"AR[{text}]" for key, text in payload.items()
                  if text != "bad"}

    writer.finalize(translated, tolerance=0)
    assert not (tmp_path / "a.ar.ass").exists()


def test_a_generous_tolerance_writes_everything(tmp_path, ass_factory):
    """This is how passthrough writes files with no translations at all."""
    source = ass_factory(tmp_path / "a.en.ass",
                         [("Default", "one"), ("Default", "two")])
    writer, payload = _writer([source])

    writer.finalize({}, tolerance=len(payload))
    assert (tmp_path / "a.ar.ass").exists()


def test_missing_lines_carry_text_and_owning_files(tmp_path, ass_factory):
    first = ass_factory(tmp_path / "a.en.ass",
                        [("Default", "shared"), ("Default", "only a")])
    second = ass_factory(tmp_path / "b.en.ass", [("Default", "shared")])
    writer, payload = _writer([first, second])

    missing = writer.finalize({}, tolerance=0)
    by_text = {info["text"]: info for info in missing.values()}
    assert set(by_text) == {"shared", "only a"}
    assert sorted(by_text["shared"]["files"]) == ["a.en.ass", "b.en.ass"]
    assert by_text["only a"]["files"] == ["a.en.ass"]


def test_finalize_does_not_rewrite_an_already_written_file(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass", [("Default", "one")])
    writer, payload = _writer([source])

    writer.write_ready(_translate_all(payload))
    writer.finalize(_translate_all(payload), tolerance=0)
    assert writer.completed == ["a.ar.ass"]


# ── Manifest records ──────────────────────────────────────────────────────────

class _RecordingManifest:
    def __init__(self):
        self.records = []

    def record_translation(self, source, details):
        self.records.append((source.name, details))


def test_a_complete_file_is_recorded_as_complete(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass", [("Default", "one")])
    manifest = _RecordingManifest()
    writer, payload = _writer([source], manifest_run=manifest)

    writer.write_ready(_translate_all(payload))
    assert manifest.records[0][1]["status"] == "complete"


def test_a_partial_file_records_its_untranslated_lines(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass",
                         [("Default", "good"), ("Default", "bad")])
    manifest = _RecordingManifest()
    writer, payload = _writer([source], manifest_run=manifest)

    translated = {key: f"AR[{text}]" for key, text in payload.items()
                  if text != "bad"}
    writer.finalize(translated, tolerance=1)

    name, details = manifest.records[0]
    assert details["status"] == "partial"
    assert details["untranslated_lines"] == ["bad"]


def test_a_skipped_file_is_recorded_as_incomplete(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass", [("Default", "bad")])
    manifest = _RecordingManifest()
    writer, payload = _writer([source], manifest_run=manifest)

    writer.finalize({}, tolerance=0)
    assert manifest.records[0][1]["status"] == "incomplete"


def test_recorded_lines_are_capped(tmp_path, ass_factory):
    """A batch where a whole chunk failed must not bloat the job record."""
    from btcli.batch import MAX_RECORDED_LINES

    cues = [("Default", f"line {index}") for index in range(MAX_RECORDED_LINES + 25)]
    source = ass_factory(tmp_path / "a.en.ass", cues)
    manifest = _RecordingManifest()
    writer, payload = _writer([source], manifest_run=manifest)

    writer.finalize({}, tolerance=0)
    assert len(manifest.records[0][1]["untranslated_lines"]) == MAX_RECORDED_LINES


def test_details_report_dedup_and_chunk_sizes(tmp_path, ass_factory):
    source = ass_factory(tmp_path / "a.en.ass",
                         [("Default", "same"), ("Default", "same"),
                          ("Default", "other")])
    writer, payload = _writer([source])

    details = writer.details(1, {}, "incomplete")
    assert details["cues"] == 3
    assert details["unique_lines"] == 2, "the repeated line collapses"
    assert details["deduplicated_lines"] == 1
    assert details["batch_chunk_sizes"] == writer.chunk_sizes
