"""End-of-job retry: re-send only the lines that are still missing.

Runs once after every folder has been processed. Because each translated line
was already written to the cache during the job, a retry costs only the missing
lines. Smaller chunks are offered because one oversized request that fails takes
every line in it down, scattering damage across many files.
"""
from __future__ import annotations

from pathlib import Path

from .config import cfg
from .logger import log
from .prompts import Abort, ask, ask_yes_no, header, is_interactive


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
    """Prompt for the retry chunk size. None means keep the current value."""
    half = max(1, current // 2)
    default = f"{half}"
    print(f"  Current chunk size: {current} lines. Smaller chunks fail less often")
    print("  and confine a failure to fewer files.")
    print("  Enter a percentage of the current size (50%), or a line count (300).")
    while True:
        answer = ask("  New chunk size", default)
        try:
            return parse_chunk_size(answer, current)
        except ValueError as exc:
            print(f"  {exc}")


def _report(results: list) -> None:
    missing = total_missing(results)
    incomplete = [r for r in results if r.get("missing")]
    header("MISSING LINES")
    print(f"  {missing} line(s) still missing across {len(incomplete)} folder(s):")
    for result in incomplete:
        where = Path(result.get("path", "?")).name or result.get("path", "?")
        print(f"    {where}: {len(result['missing'])} line(s)")
    print("  Translated lines are already cached, so a retry only sends what is missing.")


def offer_retry(results: list, api_key: str) -> list:
    """Offer to retry missing lines, looping until done or declined.

    Returns the final list of run results.
    """
    from .translate import run_translate

    results = [r for r in results if r]
    original_chunk = cfg.get("MAX_LINES_PER_CHUNK", 1000)

    try:
        while True:
            if not total_missing(results):
                return results

            _report(results)

            if not is_interactive():
                log.info("  Re-run the same command to translate the missing lines "
                         "(cached lines are reused).")
                return results

            if not ask_yes_no("  Retry the missing lines now?", True):
                log.info("  Skipped. Re-run later to finish; cached lines are reused.")
                return results

            current = cfg.get("MAX_LINES_PER_CHUNK", 1000)
            new_size = _ask_chunk_size(current)
            if new_size and new_size != current:
                cfg["MAX_LINES_PER_CHUNK"] = new_size
                log.info(f"  Retrying with chunk size {new_size} (was {current}), "
                         f"this run only")

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

            if not retried:
                return results

            before = total_missing(results)
            after = total_missing(retried)
            if after >= before:
                log.warning(f"  Retry recovered nothing ({after} still missing). "
                            f"Stopping to avoid a loop.")
                return retried
            log.success(f"  Recovered {before - after} line(s); {after} still missing")
            results = retried
    except Abort as exc:
        log.warning(f"Cancelled ({exc}). Translated lines are cached.")
        return results
    except KeyboardInterrupt:
        print()
        log.warning("Cancelled. Translated lines are cached.")
        return results
    finally:
        cfg["MAX_LINES_PER_CHUNK"] = original_chunk
