"""Shared test fixtures.

Subtitle fixtures are built programmatically rather than committed, so the repo
carries no large or third-party subtitle files. Each factory reproduces a trait
that has caused a real bug: styles that need selecting, karaoke that must be
passed through, ASS line breaks, dual-speaker cues, and embedded fonts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pysubs2
import pytest

# Make the package importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_settings():
    """Give every test a predictable, offline configuration.

    Values are restored afterwards so tests cannot leak settings into each other.
    """
    from btcli.config import cfg

    saved = dict(cfg)
    cfg.update({
        "GEMINI_API_KEY": "test-key",
        "EMBED_FONT": False,
        "USE_TRANSLATION_CACHE": True,
        "RESUME_PROMPT": False,          # never block a test on a prompt
        "PARTIAL_LINE_TOLERANCE": 10,
        "MAX_LINES_PER_CHUNK": 1000,
        "PARALLEL_COOLDOWN": 0,
        "RETRY_COOLDOWN": 0,
        "RETRY_ATTEMPTS": 2,
        "MAX_FAILED_CHUNKS": 1,
        "GEMINI_MODEL": "test-model",
        "MODEL_POOL": ["test-model"],
        "TARGET_LANGUAGE": "arabic",
        "SOURCE_LANGUAGE": "english",
    })
    yield cfg
    cfg.clear()
    cfg.update(saved)


@pytest.fixture(autouse=True)
def reset_resume_choice():
    """Clear the once-per-run resume answer between tests."""
    from btcli import translate

    translate.reset_resume_choice()
    yield
    translate.reset_resume_choice()


@pytest.fixture(autouse=True)
def reset_request_pacing():
    """Clear the request pacing clock so tests cannot wait on each other."""
    from btcli import ai

    ai.reset_pacing()
    yield
    ai.reset_pacing()


@pytest.fixture
def no_colour(monkeypatch):
    """Force plain output so assertions can match text directly."""
    from btcli import prompts

    monkeypatch.setattr(prompts.sys.stdout, "isatty", lambda: False, raising=False)
    return prompts


@pytest.fixture
def ass_factory():
    """Build an ASS file. Returns write(path, cues, styles) -> Path.

    cues are (style, text) pairs; ``text`` may contain ``\\N`` for an ASS break.
    """
    def write(path, cues, styles=("Default",), effects=None):
        subs = pysubs2.SSAFile()
        for name in styles:
            subs.styles[name] = pysubs2.SSAStyle()
        for index, (style, text) in enumerate(cues):
            event = pysubs2.SSAEvent(start=index * 1000, end=index * 1000 + 900)
            event.text = text
            event.style = style
            if effects and index in effects:
                event.effect = effects[index]
            subs.append(event)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        subs.save(str(path))
        return path
    return write


@pytest.fixture
def srt_factory():
    """Build an SRT file. Returns write(path, texts) -> Path."""
    def write(path, texts):
        lines = []
        for index, text in enumerate(texts, 1):
            start = f"00:00:{index:02d},000"
            end = f"00:00:{index:02d},900"
            lines.append(f"{index}\n{start} --> {end}\n{text}\n")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    return write


@pytest.fixture
def series(tmp_path, ass_factory):
    """A two-season series whose seasons have different styles.

    Mirrors the real case that motivated per-folder style selection.
    """
    root = tmp_path / "Test Show (2019)"
    season_one = root / "Season 01"
    season_two = root / "Season 02"

    ass_factory(season_one / "e01.en.ass",
                [("Default", "Good morning"),
                 ("Default", r"Hurry up!\NWe are late"),
                 ("Signs", "SHOP")],
                styles=("Default", "Signs"))
    ass_factory(season_one / "e02.en.ass",
                [("Default", "See you later"),
                 ("Default", r"--Here.\N--Thank you.")],
                styles=("Default", "Signs"))
    ass_factory(season_two / "e01.en.ass",
                [("Default", "Different season"),
                 ("ED-RO", "la la la")],
                styles=("Default", "ED-RO"))
    return root


@pytest.fixture
def fake_translator(monkeypatch):
    """Replace the API layer. Returns install(fail_texts=..., counter=...).

    Translations are prefixed with AR[...] so tests can tell a translated line
    from one left in the source language.
    """
    from btcli import translate as translate_module

    state = {"sent": [], "calls": 0}

    def install(fail_texts=()):
        failures = set(fail_texts)

        async def fake_run(chunks, payload, api_key, show_name,
                          source_lang, target_lang, progress_callback=None):
            state["calls"] += 1
            state["sent"].append(sum(len(chunk) for chunk in chunks))
            done = {}
            for chunk in chunks:
                done.update({key: f"AR[{text}]" for key, text in chunk.items()
                             if text not in failures})
                if progress_callback:
                    progress_callback(dict(done))
            return done

        monkeypatch.setattr(translate_module, "run_translation", fake_run)
        return state

    return install


def untranslated_lines(path):
    """Cue texts in an output file that were left in the source language."""
    return [event.text for event in pysubs2.SSAFile.load(str(path))
            if "AR[" not in event.text]
