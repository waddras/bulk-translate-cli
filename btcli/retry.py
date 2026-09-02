"""End-of-job handling for lines that are still missing.

Runs once after every folder has been processed. Because each translated line was
already written to the cache during the job, retrying costs only the missing
lines. Smaller chunks are offered because one oversized request that fails takes
every line in it down, scattering damage across many files.

Choices offered:
  retry        re-send only the missing lines, at a smaller chunk size
  show         list the untranslated lines so their value can be judged
  passthrough  write the files now, leaving those lines in the source language
  skip         leave everything cached for a later run
"""
from __future__ import annotations

from pathlib import Path

from .config import cfg
from .logger import log
from .prompts import (
    Abort,
    ask,
    ask_menu,
    bad,
    good,
    header,
    hint,
    is_interactive,
    warn,
)

MAX_SHOWN = 200


def total_missing(results: list) -> int:
    """Total still-missing unique lines across every folder that ran."""
    return sum(len(result.get("missing") or ()) for result in results)


def parse_chunk_size(raw: str, current: int) -> int:
    """Read a chunk size as a percentage of the current value, or a line count."""
    text = raw.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("enter a percentage like 50% or a line count like 300")

    if text.endswith("%"):
        body = text[:-1]
        try:
            percent = float(body)
        except ValueError:
            raise ValueError(f"'{raw}' is not a percentage")
        if not 0 < percent <= 100:
            raise ValueError("percentage must be between 1 and 100")
        return max(1, int(current * percent / 100))

    if text.isdigit():
        size = int(text)
        if size < 1:
            raise ValueError("chunk size must be at least 1")
        return size

    raise ValueError("enter a percentage like 50% or a line count like 300")


def _ask_chunk_size(current: int) -> int | None:
    """Prompt for the retry chunk size."""
    default = str(max(1, current // 2))
    hint(f"  Current chunk size: {current} lines. Smaller chunks fail less often")
    hint("  and confine a failure to fewer files.")
    hint("  Enter a percentage of the current size (50%), or a line count (300).")
    while True:
        answer = ask("  New chunk size", default)
        try:
            return parse_chunk_size(answer, current)
        except ValueError as exc:
            bad(f"  {exc}")


def _report(results: list) -> None:
    """Summarise what is missing, per folder."""
    missing = total_missing(results)
    incomplete = [r for r in results if r.get("missing")]
    header("MISSING LINES")
    warn(f"  {missing} line(s) still missing across {len(incomplete)} folder(s):")
    for result in incomplete:
        where = Path(result.get("path", "?")).name or result.get("path", "?")
        print(f"    {where}: {len(result['missing'])} line(s)")
    hint("  Translated lines are already cached, so a retry only sends what is missing.")


def _show_missing(results: list) -> None:
    """List every untranslated line, grouped by folder, with its output files."""
    from .prompts import ITEM, paint

    header("UNTRANSLATED LINES")
    shown = 0
    for result in [r for r in results if r.get("missing")]:
        where = Path(result.get("path", "?")).name or result.get("path", "?")
        entries = result["missing"]
        print("\n  " + paint(f"{where}  ({len(entries)} line(s))", ITEM))

        for info in sorted(entries.values(), key=lambda i: i.get("text", "")):
            if shown >= MAX_SHOWN:
                hint(f"    ... and {len(entries) - shown} more in this folder")
                break
            text = (info.get("text") or "").replace("\n", " / ")
            files = info.get("files") or []
            where_used = f"  [{', '.join(files)}]" if len(files) > 1 else ""
            print(f"    {text}" + paint(where_used, ITEM))
            shown += 1

    print()
    hint("  Lines shown as source text. '/' marks a line break inside one cue.")
    if total_missing(results) > MAX_SHOWN:
        hint(f"  Output truncated at {MAX_SHOWN} lines; "
             f"the manifest records the rest.")


def _passthrough(results: list) -> list:
    """Write every incomplete file now, leaving missing lines in the source language."""
    from .translate import run_translate

    written = []
    for result in [r for r in results if r.get("missing")]:
        target = result.get("path", "")
        header(f"PASSTHROUGH - {Path(target).name or target}")
        try:
            again = run_translate(
                path=target,
                lang=result.get("lang", cfg.get("TARGET_LANGUAGE", "arabic")),
                input_type="sub",
                suffix=result.get("suffix"),
                force_srt=result.get("force_srt", False),
                show_name=result.get("show_name", ""),
                keep_styles=result.get("keep_styles"),
                passthrough_styles=result.get("passthrough_styles"),
                preset_files=[str(f) for f in result.get("files", [])],
                use_cache=True,
                write_only=True,      # no API calls; assemble from cache
                allow_resume_prompt=False,
            )
        except KeyboardInterrupt:
            print()
            log.warning("Interrupted. Translated lines are cached.")
            return results
        except Exception as exc:
            log.error(f"Passthrough failed for {target}: {exc}")
            again = None
        if again:
            written.append(again)

    if written:
        good("  Files written with untranslated lines left in the source language.")
        hint("  Re-run later to finish them; cached lines are reused and the "
             "files are rewritten complete.")
    return written or results


def _retry_once(results: list) -> list:
    """Re-send only the missing lines for each incomplete folder."""
    from .translate import run_translate

    retried = []
    for result in [r for r in results if r.get("missing")]:
        target = result.get("path", "")
        header(f"RETRY - {Path(target).name or target}")
        try:
            again = run_translate(
                path=target,
                lang=result.get("lang", cfg.get("TARGET_LANGUAGE", "arabic")),
                input_type="sub",              # sources are already extracted
                suffix=result.get("suffix"),
                force_srt=result.get("force_srt", False),
                show_name=result.get("show_name", ""),
                keep_styles=result.get("keep_styles"),
                passthrough_styles=result.get("passthrough_styles"),
                preset_files=[str(f) for f in result.get("files", [])],
                use_cache=True,               # cached lines are not re-sent
                allow_resume_prompt=False,
            )
        except KeyboardInterrupt:
            print()
            log.warning("Retry interrupted. Translated lines are cached.")
            return results
        except Exception as exc:
            log.error(f"Retry failed for {target}: {exc}")
            again = None
        if again:
            retried.append(again)
    return retried


def offer_retry(results: list, api_key: str = "") -> list:
    """Offer retry / show / passthrough / skip, looping until resolved.

    Returns the final list of run results.
    """
    results = [r for r in results if r]
    original_chunk = cfg.get("MAX_LINES_PER_CHUNK", 1000)
    reported = False

    try:
        while True:
            if not total_missing(results):
                return results

            if not reported:
                _report(results)
                reported = True

            if not is_interactive():
                log.info("  Re-run the same command to translate the missing lines "
                         "(cached lines are reused).")
                return results

            choice = ask_menu(
                "  What next?",
                [
                    ("y", "retry the missing lines now, at a smaller chunk size"),
                    ("s", "show the untranslated lines so you can judge them"),
                    ("p", "passthrough - write the files now, these lines left "
                          "in the source language"),
                    ("n", "nothing - lines stay cached for a later run"),
                ],
                default="y",
            )

            if choice == "s":
                _show_missing(results)
                continue        # back to the menu

            if choice == "n":
                log.info("  Skipped. Re-run later to finish; cached lines are reused.")
                return results

            if choice == "p":
                return _passthrough(results)

            current = cfg.get("MAX_LINES_PER_CHUNK", 1000)
            new_size = _ask_chunk_size(current)
            if new_size and new_size != current:
                cfg["MAX_LINES_PER_CHUNK"] = new_size
                log.info(f"  Retrying with chunk size {new_size} (was {current}), "
                         f"this run only")

            before = total_missing(results)
            retried = _retry_once(results)
            if not retried:
                return results

            after = total_missing(retried)
            if after >= before:
                warn(f"  Retry recovered nothing ({after} still missing).")
                results = retried
                reported = False
                # Fall through to the menu so passthrough is still an option.
                continue
            good(f"  Recovered {before - after} line(s); {after} still missing")
            results = retried
            reported = False
    except Abort as exc:
        log.warning(f"Cancelled ({exc}). Translated lines are cached.")
        return results
    except KeyboardInterrupt:
        print()
        log.warning("Cancelled. Translated lines are cached.")
        return results
    finally:
        cfg["MAX_LINES_PER_CHUNK"] = original_chunk
