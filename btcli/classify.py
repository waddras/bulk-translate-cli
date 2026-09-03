"""Ask Gemini which subtitle track and which ASS styles are real dialogue.

A release with forty styles — ``Base01``, ``Base01 - Overlap``, ``EdEnglish``,
``NodameOP``, ``sign1``..``sign10``, ``letter1``, ``gyabo``, ``why`` — cannot be
picked apart by hand, and style *names* alone are a weak signal. This module
sends the model enough evidence to judge, in ONE call per folder, and turns the
reply into the ``(keep, passthrough)`` pair the rest of btcli already speaks.

WHAT MAKES THE JUDGEMENT POSSIBLE
    Cue count is nearly decisive: dialogue runs to hundreds of lines, a sign has
    four. So each style is described by its count, two or three sample lines, and
    whether its cues carry ``\\pos`` (a sign, placed on screen) or ``\\k``
    (karaoke, so an opening or ending). Track metadata comes too, since titles
    like "Signs & Songs" say plainly what a track is for.

    The candidate list carries one entry per subtitle TRACK, each with its own
    styles, because styles only exist once a track is chosen — that is why the
    track and the styles have to be decided by the same call rather than two.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    * **No pacing.** ``ai.pace_requests`` would stall the interactive
      questionnaire for PARALLEL_COOLDOWN seconds per folder, and these calls are
      one small request each. They also do not touch ``ai._last_api_call``, so
      they never delay the first translation chunk.
    * **No model ladder.** The model is pinned by AI_SELECT_MODEL, because the
      point of choosing it is its request-per-day budget. Walking the ladder on
      failure would spend a different model's quota to answer a question the
      user can answer instantly.
    * **One attempt.** With no cooldown and a pinned model, an immediate retry of
      a 429 is pointless. A failed call returns None and the caller asks the
      human instead, which is the better fallback in a guided flow.
    * **Never widens scope.** A reply naming no style btcli can recognise is
      discarded whole. Guessing "translate everything" would quietly spend the
      user's quota on signs and karaoke.

VALIDATION
    Same discipline as the translation IDs in ``ai._normalize_result``: the track
    must be one that was offered, and every style name must be one that actually
    exists in that track. Anything invented is dropped and logged. The model
    proposes; it does not get to introduce names.

FLOW
    ``style_facts(path)``     evidence for one subtitle file, pure pysubs2
    ``choose(candidates...)`` one API call, returns a verdict dict or None
    caller (interactive.py) shows the verdict and asks the user to confirm it
"""
from __future__ import annotations

import asyncio
import json

from .config import cfg
from .logger import log

# Sample lines sent per style. Two or three is enough to tell "Yeah, I know."
# from "TOKYO — 3 YEARS EARLIER", and keeps the payload small enough that forty
# styles still fit comfortably in one request.
SAMPLE_LINES_PER_STYLE = 3

# Samples are trimmed to this, so one pathological karaoke line cannot dominate.
MAX_SAMPLE_CHARS = 120

# Guard against a pathological file with dozens of subtitle tracks: each costs a
# local ffmpeg extraction before we can describe it.
MAX_TRACK_CANDIDATES = 6

# Everything not named as dialogue is passed through untouched rather than
# dropped. "+ALL" is the existing sentinel for "every style not in keep", which
# styles.resolve_styles_with_files expands once the real style list is known.
PASSTHROUGH_REST = "+ALL"


def default_instruction() -> str:
    """The user's instruction to the model, from settings."""
    return cfg.get("AI_SELECT_PROMPT", "") or ""


def selected_model() -> str:
    """The model pinned for verdict calls."""
    return cfg.get("AI_SELECT_MODEL", "gemini-3.5-flash-lite")


# ── Evidence ──────────────────────────────────────────────────────────────────

def style_facts(path) -> list:
    """Describe every style in one subtitle file, as evidence for the model.

    Returns a list of {name, cues, positioned, karaoke, samples}, ordered by cue
    count descending so the likeliest dialogue style is read first — the order is
    itself a hint, and it keeps the interesting styles at the top if a very long
    list ever has to be truncated.

    A file that cannot be parsed yields an empty list rather than raising: the
    caller falls back to asking the user, which is not worth an exception.
    """
    import pysubs2

    from .srt_pre import _clean_event_text

    try:
        subs = pysubs2.SSAFile.load(str(path))
    except Exception as exc:
        log.detail(f"    Could not read {path} for style facts: {exc}")
        return []

    facts: dict = {}
    for name in subs.styles:
        facts[name] = {"name": name, "cues": 0, "positioned": False,
                       "karaoke": False, "samples": []}

    for event in subs:
        name = getattr(event, "style", None)
        if name is None:
            continue
        entry = facts.setdefault(
            name, {"name": name, "cues": 0, "positioned": False,
                   "karaoke": False, "samples": []})
        entry["cues"] += 1

        raw = event.text or ""
        # Checked on the raw text, before tags are stripped: these two tags are
        # the whole signal. \pos means the line is pinned somewhere on screen,
        # which is what a sign is; \k is karaoke timing, so an OP or ED.
        if "\\pos" in raw or "\\move" in raw:
            entry["positioned"] = True
        if "\\k" in raw or "[fx]" in (getattr(event, "effect", "") or ""):
            entry["karaoke"] = True

        if len(entry["samples"]) < SAMPLE_LINES_PER_STYLE:
            text = " ".join(_clean_event_text(raw).split())
            if text:
                entry["samples"].append(text[:MAX_SAMPLE_CHARS])

    return sorted(facts.values(), key=lambda item: item["cues"], reverse=True)


# ── Prompt ────────────────────────────────────────────────────────────────────

def _verdict_schema() -> dict:
    """Schema pinning the reply to one verdict object.

    Only dialogue_styles is required: a folder of subtitle files has no track to
    choose, and a model that omits an optional field is easier to handle than one
    that invents a value for it.
    """
    return {
        "type": "OBJECT",
        "properties": {
            "track": {"type": "INTEGER"},
            "dialogue_styles": {"type": "ARRAY", "items": {"type": "STRING"}},
            "reason": {"type": "STRING"},
        },
        "required": ["dialogue_styles"],
    }


def _generation_config() -> dict:
    """Generation config for a verdict call.

    Its own, not ai._generation_config(): that one pins the reply to an array of
    {id, text} for translation, which would force this answer into the wrong
    shape entirely. Temperature 0 because this is a classification, not writing.
    """
    return {
        "temperature": 0.0,
        "responseMimeType": "application/json",
        "responseSchema": _verdict_schema(),
    }


def _output_contract(multi_track: bool) -> str:
    """The reply format required, appended after the user's instruction.

    Placed last and declared as overriding, exactly as ai._output_contract is,
    so that a hand-edited AI_SELECT_PROMPT cannot change the reply shape and
    break parsing. The instruction says what to choose; this says how to answer.
    """
    track_rule = (
        "- Set track to the index of the ONE track that carries the dialogue.\n"
        if multi_track else
        "- Omit track. These are subtitle files, so there is no track to choose.\n"
    )
    return (
        "BTCLI OUTPUT CONTRACT (this overrides any earlier output-format wording):\n"
        "- Return a single JSON object with the fields: track, dialogue_styles, reason.\n"
        + track_rule +
        "- dialogue_styles lists the style names, copied EXACTLY as given, that carry\n"
        "  spoken dialogue in that track. Never invent, translate, or reformat a name.\n"
        "- Leave dialogue_styles empty if none of the styles is dialogue.\n"
        "- Do not list styles that only carry signs, titles, location captions, letters,\n"
        "  inserts, credits, opening or ending songs, or karaoke.\n"
        "- reason is one short sentence explaining the choice.\n"
        "- Return JSON only, with no markdown or commentary around the object.\n"
    )


def _payload(candidates: list) -> dict:
    """The evidence, as the model receives it."""
    tracks = []
    for candidate in candidates:
        entry = {
            "styles": [
                {
                    "name": fact["name"],
                    "cues": fact["cues"],
                    "positioned": fact["positioned"],
                    "karaoke": fact["karaoke"],
                    "samples": fact["samples"],
                }
                for fact in candidate.get("styles", [])
            ],
        }
        if candidate.get("index") is not None:
            entry["track"] = candidate["index"]
            entry["codec"] = candidate.get("codec", "")
            entry["language"] = candidate.get("language", "")
            entry["title"] = candidate.get("title", "")
        tracks.append(entry)
    return {"tracks": tracks}


def _build_prompt(candidates: list, instruction: str, source_lang: str) -> str:
    """Assemble instruction + contract + evidence."""
    multi_track = any(c.get("index") is not None for c in candidates)
    base = instruction.strip() or default_instruction().strip()
    return (
        f"You are choosing which {source_lang} subtitle content is worth translating.\n\n"
        f"{base}\n\n"
        "Each style below is described by how many cues use it, whether those cues are\n"
        "positioned on screen (a sign) or carry karaoke timing (an opening or ending),\n"
        "and a few sample lines. Cue count is the strongest signal: real dialogue runs\n"
        "to hundreds of cues, a sign or a title has a handful.\n\n"
        + _output_contract(multi_track)
        + "\nEvidence:\n"
        + json.dumps(_payload(candidates), ensure_ascii=False, indent=1)
    )


# ── Validation ────────────────────────────────────────────────────────────────

def _unwrap(result):
    """The verdict object, however the model wrapped it."""
    if isinstance(result, list):
        # A model asked for one object sometimes sends a one-element array.
        result = next((item for item in result if isinstance(item, dict)), None)
    if not isinstance(result, dict):
        return None
    # Or wraps it under a key of its own choosing.
    if "dialogue_styles" not in result:
        for value in result.values():
            if isinstance(value, dict) and "dialogue_styles" in value:
                return value
        return None
    return result


def _pick_candidate(candidates: list, track):
    """The candidate the verdict refers to, or None if it named an unknown track.

    A single candidate with no index is the subtitle-files case: any track the
    model volunteered is irrelevant, so it is ignored rather than rejected.
    """
    indexed = [c for c in candidates if c.get("index") is not None]
    if not indexed:
        return candidates[0] if candidates else None
    if isinstance(track, bool) or not isinstance(track, int):
        # No usable track, but one candidate means there was no choice to make.
        return indexed[0] if len(indexed) == 1 else None
    for candidate in indexed:
        if candidate["index"] == track:
            return candidate
    log.detail(f"    Ignoring verdict for unknown track {track}")
    return None


def _accepted_styles(names, available: list) -> list:
    """Style names from the reply that genuinely exist, in the given order.

    Unknown names are dropped and logged rather than trusted — the same rule the
    translation path applies to inline IDs. A case-insensitive match is allowed
    because the model echoing "default" for "Default" is a formatting slip, not
    an invented style; anything else is.
    """
    if not isinstance(names, list):
        return []
    lookup = {name.casefold(): name for name in available}
    accepted, seen = [], set()
    for name in names:
        if not isinstance(name, str):
            continue
        real = name if name in available else lookup.get(name.casefold())
        if real is None:
            log.detail(f"    Ignoring style the file does not have: {name!r}")
            continue
        if real not in seen:
            seen.add(real)
            accepted.append(real)
    return accepted


def validate(result, candidates: list) -> dict | None:
    """Turn a raw reply into a verdict, or None if it cannot be trusted.

    Returns {track, keep, passthrough, reason, styles}: *keep* is concrete style
    names, *passthrough* is the "+ALL" sentinel meaning everything else, and
    *styles* is the style list it was judged against, so a cached verdict can
    later be checked against what is on disk.
    """
    verdict = _unwrap(result)
    if verdict is None:
        log.detail("    Verdict reply was not an object btcli could read")
        return None

    candidate = _pick_candidate(candidates, verdict.get("track"))
    if candidate is None:
        return None

    available = [fact["name"] for fact in candidate.get("styles", [])]
    keep = _accepted_styles(verdict.get("dialogue_styles"), available)
    if not keep:
        log.detail("    Verdict named no style this track actually has")
        return None

    reason = verdict.get("reason")
    return {
        "track": candidate.get("index"),
        "keep": keep,
        "passthrough": [PASSTHROUGH_REST],
        "reason": reason.strip() if isinstance(reason, str) else "",
        "styles": available,
    }


# ── The call ──────────────────────────────────────────────────────────────────

async def _request(prompt: str, api_key: str, model: str):
    """One request, on the pinned model, with no pacing and no retry."""
    import httpx

    from .ai import _call_gemini

    async with httpx.AsyncClient() as client:
        return await _call_gemini(client, prompt, api_key, model=model,
                                  gen_config=_generation_config())


def choose(candidates: list, api_key: str, *, instruction: str = "",
           source_lang: str = "english") -> dict | None:
    """Ask the model to choose a track and its dialogue styles. None on failure.

    Synchronous, because the guided flow that calls it is. Every failure path —
    no candidates, no styles, no key, a refused call, an untrustworthy reply —
    returns None, and the caller asks the user instead.
    """
    usable = [c for c in candidates if c.get("styles")][:MAX_TRACK_CANDIDATES]
    if not usable:
        log.detail("    No styles to classify")
        return None
    if not api_key:
        log.detail("    No API key available for style selection")
        return None

    model = selected_model()
    prompt = _build_prompt(usable, instruction, source_lang)
    log.detail(f"    Asking {model} to choose from "
               f"{sum(len(c['styles']) for c in usable)} style(s) "
               f"across {len(usable)} track(s)")

    try:
        result = asyncio.run(_request(prompt, api_key, model))
    except Exception as exc:
        log.detail(f"    Style selection call failed: {exc}")
        return None

    if result is None:
        return None

    verdict = validate(result, usable)
    if verdict is not None:
        verdict["model"] = model
    return verdict


def describe(verdict: dict) -> str:
    """The verdict as a one-line style selection, for summaries and manifests."""
    return ",".join(verdict.get("keep", [])) + f",{PASSTHROUGH_REST}"
