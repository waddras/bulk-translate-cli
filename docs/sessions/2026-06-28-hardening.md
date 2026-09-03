# Session archive — hardening pass

Record of the session that took `main` from `7254cef` to `8263dc4` (25 commits).
Kept for provenance: why things are the way they are, and what was tried and
rejected. For current state and open work see `../PROJECT-STATE.md`.

## How it started

A real corruption report: translated lines were shifted, line 3's text landing
on line 1. Investigation ruled out `json_repair` and the reassembly step, and
traced it to the model returning values shifted under valid-looking keys — the
response *looked* well-formed, so nothing rejected it.

Fix: every payload item now carries an inline `<BTCLI_ID:NNNNNN>` token inside
its own text as well as an `id` field, and a translation is accepted only when
the two agree and the ID was one the chunk asked for. That validation is the
backbone of everything after it.

Follow-on work from the same thread: `.btcli.json` manifest so re-runs stop
re-extracting subtitles, incremental output, `--files-per-call`, and an SRT fix
where two-line cues were merging and reversing (`wrap_rtl` needed a real `\n`
separator for SRT, not `\N`).

## Then a 14-item review

Asked for an assessment of the whole program. Items 12 and 13 were parked by the
user; the other 12 were completed.

| # | Item | Outcome |
|---|---|---|
| 1 | No tests at all | 329 tests, CI on 3.9 + 3.12 |
| 2 | No config validation | `validate.py`, `--check-settings`, `--strict` |
| 3 | `PROMPT_TEMPLATE` contradicted the code | rewritten to defer to the contract |
| 4 | `translate.py` unwieldy | `run_translate` 301→116, `_translate_batch` 202→85 |
| 5 | `btcli update` brittle | fast-forward only, `--branch`, `--stash`, `--check` |
| 6 | No packaging | `pyproject.toml`, `btcli` entry point, wheel tested in CI |
| 7 | No dry-run | `preview.py`, `--dry-run` |
| 8 | 4 MB sample subtitles committed | removed; fixtures built programmatically |
| 9 | README stale | 149 → 507 lines, plus tests that keep it honest |
| 10 | Cache/manifest grew forever | `btcli prune` |
| 11 | Three cooldown implementations | one `pace_requests()`, one `backoff_before_retry()` |
| 14 | `fix` could not strip embedded fonts | `font-strip` |

## Bugs found while doing the above

Several were found by the work rather than reported:

- **`local_changes` ate the first character of every filename.** It parsed
  `git status --porcelain`, but the helper stripped output, removing the leading
  status space — so `btcli/main.py` became `tcli/main.py`. Replaced with
  `git diff --name-only HEAD`.
- **`--single-branch` clones could not switch branches.** Neither DWIM checkout
  nor `--track` works when the remote's fetch refspec doesn't cover the branch.
  Root cause was the narrow refspec; `update` now widens it once, which also
  repairs plain `git pull` for the user.
- **Settings merge produced `,,`** when a config ended with a trailing comma.
- **`_strip_fonts_section` deleted `[Events]`** when a `[Fonts]` section preceded
  dialogue — the old regex was `\n?\[Fonts\]\n.*` with DOTALL. Rewritten as a
  line scanner.
- **`FILES_PER_BATCH` was unshipped.** Read by the code and named in an error
  message telling users to lower it, but absent from `settings.default.conf`, so
  `btcli update` could never add it to anyone's config. Found by the new
  docs-drift test.
- **Two dead `retry_attempts` reads** left from the model-ladder work, implying
  `RETRY_ATTEMPTS` was authoritative where `effective_attempts()` decides.
- **Retry backoff was inconsistent.** Four sites scaled by attempt number, two
  were flat — and two of those sat in the *same loop*, so a 429 backed off
  linearly while a parse failure backed off flat. `RETRY_COOLDOWN` is documented
  as "multiplied by attempt number", so the flat ones were wrong.
- **`--check-settings` called a warned config "good"**, printing the all-clear
  directly beneath its own warnings. Reported by the user, who reasonably asked
  whether the config was actually fine.
- **Duplicate JSON keys were invisible.** The user had `GEMINI_MODEL` declared
  twice; JSON keeps the last and reports nothing, and validation only ever saw
  the parsed dict. Now detected from the raw file.

## Decisions, including what was rejected

- **Test fixtures are built programmatically, not committed.** Two 4 MB
  third-party `.ass` files were deleted rather than trimmed and kept — licensing
  and repo bloat.
- **ANSI colour lives in `prompts.py`, not rich.** The logger owns a rich console
  on stderr; mixing writers reorders output.
- **`font-strip` is excluded from `--apply all`** — it is the opposite of `font`,
  and asking for both is refused.
- **Cache pruning is by "source line no longer exists on disk"**, not by age or
  size. Time-based expiry was rejected because it would throw away work you could
  still resume from.
- **`prune` reports by default and needs `--apply`** — destructive-by-default was
  rejected.
- **`NullManifestRun`** rather than scattering `if dry_run` guards.
- **Dry run with `-i vid` stops before extraction**, because extraction writes
  files. Extracting anyway, or guessing cue counts, were both rejected — it says
  plainly that counts are unavailable and suggests `-i sub`.
- **Pacing is per-batch in chunked mode**, preserving `PARALLEL_CHUNKS` firing
  together; per-request pacing was rejected.
- **`--reset` and `--dedupe` are separate commands.** They want opposite things:
  reset restores the shipped default (and will discard a deliberate choice),
  dedupe keeps the value already in effect. Both edit raw text rather than
  re-serialising, because a config full of section-banner comments is the normal
  case here and `json.dumps` would destroy it.
- **Settings repair never overwrites silently** — `.bak` first, refuse to write
  anything unparseable, and dedupe additionally refuses if the effective settings
  would change.

## Process notes

- **Mutation testing earned its keep.** It caught a progress-bar test that
  passed for the wrong reason, and a subcommand-summary check that searched the
  whole output — so deleting `prune` from the command listing failed nothing,
  because the word still appeared in the usage examples below.
- One mutation run reported MISSED incorrectly: passing `-q` on top of the
  configured `-q` gives `-qq`, which prints only `FAILED`, and the check was
  grepping lowercase. Re-run on exit codes.
- **Uncommitted work was lost once.** `git checkout --` was used to restore files
  after a mutation, but the README rewrite had not been committed, so it was
  wiped and had to be redone. Commit before mutating.

## Verified on the user's install

`btcli update --branch main` fast-forwarded `7254cef → 8263dc4` (47 files),
`--dedupe` collapsed the duplicated `GEMINI_MODEL` keeping
`gemini-3.5-flash-lite`, and `--reset PROMPT_TEMPLATE` replaced the stale
template. A prompt dump confirmed the contradictory
"Return a valid JSON object with the EXACT same keys" was gone and replaced by
"The exact reply format is specified by the output contract that follows this
prompt". All 7 configured model names were confirmed to exist against
`GET /v1beta/models`.
