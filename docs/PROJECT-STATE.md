# btcli — project state

Paste this into a new chat as context. Describes `main`; no commit hash, because
a doc cannot name the commit that contains it and the reference always went
stale. Use `git log --oneline -5` for where `main` actually is.

## What this is

`bulk-translate-cli` (`btcli`) — bulk-translates SRT/ASS subtitles, English→Arabic
by default, via the Gemini API. Installed at **`/opt/btcli`** as a git checkout,
run as `btcli` (alias for `PYTHONPATH=/opt/btcli python3 -m btcli`).

Only this repo is relevant. The `waddras/bulk-translate` web-UI repo is retired.

## Working preferences

- **Diagnosed items: just fix them and commit.** Anything already worked out and
  written down here does not need re-confirming — implement it, test it, push it.
- **Still discuss first when the fix is not settled.** A change with open design
  questions, or one whose diagnosis says "verify on a real run first", gets
  discussed before any code.
- **Be concise.** No extra tables, summaries or explanation beyond what was asked.
- **Say where commands run**, in bold — e.g. **On the box (`/opt/btcli`):**
- **Long-running SSH commands:** warn up front and give literal `tmux` commands.

## State

`main` = all work merged, nothing outstanding unpushed.
341 tests, pyflakes clean. CI: py3.9 + 3.12, compileall, pyflakes, pytest,
help-page render, wheel build + entry-point check.

Every module carries a docstring explaining what the file does and how it flows;
docstring coverage is 95%. The 5% left are no-op stubs, trivial properties and
nested closures whose parent explains them. Read the module docstring before
changing a file — several record why the obvious alternative was rejected.

## Layout

```
btcli/
  main.py         CLI surface: argparse, help text, dispatch
  config.py       settings loader; DEFAULT_SETTINGS is the real default set
  validate.py     settings validation (--check-settings), duplicate-key detection
  interactive.py  guided mode, back-navigation via BACK sentinel
  discover.py     file discovery
  probe.py        probe flow
  auto.py         --auto track/style heuristics
  extract.py      ffmpeg extraction, track merging
  manifest.py     .btcli.json job records; ManifestRun / NullManifestRun
  cache.py        .btcli-cache.json translation cache
  srt_pre.py      parse SRT/ASS, clean text, keep tags
  styles.py       style selection, detect_karaoke_styles
  blob.py         dedup + chunking; assigns FFLLLL keys
  ai.py           Gemini API, model ladder, pacing, response validation
  translate.py    run_translate orchestrator + _translate_batch
  batch.py        BatchWriter — incremental writing as responses arrive
  retry.py        retry rounds for missing lines
  sub_post.py     write output: RTL, styles, font embedding
  fix.py          repair flow (no API calls)
  preview.py      --dry-run reporting
  prune.py        reclaim cache/manifest state
  update.py       self-update, settings merge, --reset / --dedupe
  prompts.py      coloured terminal prompts
  logger.py       levelled output
  setup_key.py    first-run API key setup
```

## Mechanisms worth knowing before changing anything

**Line-shift protection.** Every payload item carries an inline
`<BTCLI_ID:NNNNNN>` token inside its text *as well as* an `id` field.
`ai._normalize_result` accepts a translation only if the two agree, the ID was
one this chunk asked for, and it hasn't already appeared. Rejected keys are left
for targeted retry. This is what fixed the original bug where translations
landed on the wrong cues — do not weaken it.

**Output contract.** `ai._output_contract()` is appended after
`PROMPT_TEMPLATE` and states it overrides any earlier output-format wording.
The template must therefore **not** specify a reply shape. `{json_blob}` must be
present in the template or btcli discards it entirely and falls back to a
built-in one-liner; its position no longer matters (it is replaced with an empty
string and the payload appended after the contract).

**Model ladder.** `GEMINI_MODEL` + `MODEL_POOL`, merged and de-duplicated.
A failure switches model immediately; `pace_requests()` cooldown applies only
once the whole ladder has been tried. `effective_attempts()` =
`max(RETRY_ATTEMPTS, len(ladder))`, so every model always gets a turn.

**Cache.** `.btcli-cache.json` keyed by **source text**, so renames and
reordering cannot corrupt it. `series_root_for()` maps a `Season NN` folder to
its parent, so **all seasons of a series share one cache**. Written as each
response arrives, so an interrupted run loses nothing.

Because it is shared and grows during the run, "the cache holds this line" is
**not** the same question as "an earlier run translated this line". The
first-open snapshot (`cache._snapshots`, `from_earlier_run()`) answers the
second, and anything user-facing must use it — see recently-fixed item 1.

**Manifest.** `.btcli.json` per directory; records track/styles chosen and what
was extracted. Reuse requires both a manifest record *and* the file still
existing on disk. `--force` ignores it.

**Settings.** User values are never overwritten. `btcli update` only adds
missing keys. `--reset KEYS` restores shipped defaults; `--dedupe` collapses
repeated keys keeping the value already in effect. Both edit raw text so
comments survive, write a `.bak`, and refuse to produce an unparseable file.

## Current user configuration

`GEMINI_MODEL: gemini-3.5-flash-lite`, a 7-model ladder, `RETRY_ATTEMPTS: 5`,
`MAX_FAILED_CHUNKS: 2`, `PARTIAL_LINE_TOLERANCE: 10`, `RESUME_PROMPT: true`,
`EMBED_FONT: true`. Verified all 7 model names exist against
`GET /v1beta/models`.

Nothing has ever been verified against the live API by tests — all 341 use a
fake translator. Real-world confidence comes only from actual runs.

## Recently fixed

Original item numbers are kept, so 1, 2 and 4 are here and 3 and 5 are still
open below.

### 1. Resume prompt fired mid-run — FIXED

The prompt appeared during season 2 of a 3-season job, stalling an unattended
run on a keypress, and claimed the lines came from "an earlier run" when they
were this run's own work from minutes earlier.

Cause: `_resume_choice` latched only when `_ask_resume` was actually *called*,
and it was only called when `cached_hits` was non-empty. A fresh series' season 1
found an empty cache, so it never asked and never latched; it then filled the
shared cache, and season 2's common lines ("Yes.", "Thank you.") triggered the
prompt mid-run.

Fixed with two guards, since the snapshot alone left a hole:

- **First-open snapshot** (`cache.py`). `_snapshots` records the key set each
  cache file held the first time the process opened it, and never refreshes it.
  `from_earlier_run()` is what the prompt consults, so a run can no longer
  mistake its own work for an earlier run's.
- **Work-started latch** (`translate.py`). `mark_work_started()` fires when the
  first chunk is dispatched, after which the prompt is skipped and reuse
  assumed. Needed because a genuine re-run whose first batch happens to be
  all-new lines would otherwise still reach the question at batch 2, with the
  run committed and nobody watching.

Also changed: declining now re-translates only the lines from earlier runs and
keeps what the current run has paid for — it used to re-send everything,
spending quota twice within one job.

Note for future changes: `_resolve_cache` runs **per batch** (`FILES_PER_BATCH`,
25) and interactive mode loops `run_translate` per folder, so anything that
prompts from there can fire long after the run began.

### 2. "not written because 0 unique line(s) remain untranslated" — FIXED

Nonsense warning on files whose every style is in the passthrough list
(`→ 0 cues`), and the file was not written at all.

Three places were involved, not the two originally diagnosed:

- `batch.write_ready` — `if not required or not required.issubset(...)` treated
  an empty requirement as "not ready yet". An empty set is a subset of anything,
  so dropping the `not required` clause makes such a file ready by definition.
- `batch.finalize` — the failure message reported `len(missing_keys)` = 0. It now
  distinguishes "no cues to write at all" from untranslated lines.
- `sub_post.reassemble_files` — **the one the original diagnosis missed.** It
  bailed on `if not cues` before reaching `build_ass_output`, which is what
  actually carries passthrough cues over from the source. Without this the file
  still would not have been written. Guarded by `_passthrough_cue_count()`, and
  only for ASS: SRT has no styles, so it has nothing to carry.

Written files are recorded as `complete`, because `manifest.finish()` reads that
status to decide whether the job succeeded and a file needing no translation is
not a shortfall.

### 4. RETRY_ATTEMPTS warning was mis-levelled — FIXED

`ai.effective_attempts()` raises the value unconditionally, so the warning
demanded action that was impossible on every single command — and `--strict`
refused to run at all over it.

`check_settings()` now returns **three** lists: `(errors, warnings, notes)`.
A note is logged with `log.info`, never blocks even under `--strict`, and does
not withhold the "looks good" verdict. Callers unpacking two values need
updating; `report_settings` is the only one in `btcli/`.

The general rule this encodes: a warning that asks for nothing trains people to
ignore warnings that ask for something.

## Open items

### 3. "Ignoring unexpected inline ID" churn (inefficiency)

~7% of a chunk rejected and retried (e.g. 634 lines → 589 ok, 45 retried).
Validation is working correctly; this is not a correctness problem.

Cause: keys are `FF` + the cue's **position in the file**, and dedup means
repeated lines get no payload entry — so the ID list the model receives has
holes (`1,2,3,5,7,9`). The model interpolates the missing numbers, each invented
item is rejected, and it tends to displace a real one.

Proposed: number the **wire** IDs contiguously per chunk and keep the mapping to
the real `FFLLLL` tag internally. Validation stays equally strict, just against
dense IDs. Verify the hypothesis first by logging rejected IDs against the
chunk's expected set on one real run.

### 5. Next feature: one-call Gemini classification during probe

**Intent:** during the probing phase, send everything to Gemini in a **single
API call** and have it name which styles are real dialogue and which track to
use. Goal is regular dialogue only — no OP/ED, no signs, no inserts.

Motivating case: a series with ~40 styles (`Base01`, `Base01 - Overlap`,
`Base04`, `EdEnglish`, `Nodame Alt/Background/Insert EN/Insert JP/Past/Primary/
Thought/Thought Alt`, `NodameED`, `NodameOP`, `OpEnglish`, `glaucue`, `glaucue2`,
`gross`, `gyabo`, `huh`, `letter1/2`, `sign1`–`sign10`, `signa`–`signg`,
`signs`, `signs-school`, `signs-skinny`, `why`) — unpickable by hand.

Design notes from discussion:
- Style *names* alone are a weak signal. Send per style: name, cue count,
  2–3 sample lines, and whether cues carry `\pos` (signs) or `\k` (karaoke).
  Cue count is nearly decisive — dialogue has hundreds, signs have a handful.
- Track metadata (title/language from ffprobe) goes in the same call.
- **Validate the reply against the real style/track sets** — reject hallucinated
  names, same discipline as the translation IDs.
- **Cache the verdict** in `.btcli.json`, asked once per series.
- **Must not silently widen scope** — propose, user confirms; visible in
  `--dry-run` before quota is spent.
- **Fall back** to the existing `auto.py` heuristics if the call fails.
- Open questions: count thoughts/monologue as dialogue (probably yes); exclude
  inserts/letters (probably yes); opt-in or eventually default for `--auto`.

Existing overlap to respect: `auto.py` track keyword matching,
`styles.detect_karaoke_styles()`, `KEEP_TOP_STYLES` (top N by unique count).

### Parked at user's request

- RPM/RPD quota tracking and pacing (react-to-429 only today).
- whisper.cpp transcription for hardsubbed / subtitle-less files.
