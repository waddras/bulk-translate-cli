# btcli — project state

Paste this into a new chat as context. Current as of commit `8263dc4` on `main`.

## What this is

`bulk-translate-cli` (`btcli`) — bulk-translates SRT/ASS subtitles, English→Arabic
by default, via the Gemini API. Installed at **`/opt/btcli`** as a git checkout,
run as `btcli` (alias for `PYTHONPATH=/opt/btcli python3 -m btcli`).

Only this repo is relevant. The `waddras/bulk-translate` web-UI repo is retired.

## Working preferences

- **Discuss first, never auto-code.** When a bug is reported or a change
  discussed, only discuss it. Ask "want me to code this?" and wait for an
  explicit "code" / "yes" / "go".
- **Be concise.** No extra tables, summaries or explanation beyond what was asked.
- **Say where commands run**, in bold — e.g. **On the box (`/opt/btcli`):**
- **Long-running SSH commands:** warn up front and give literal `tmux` commands.

## State

`main` = 25 commits, all work merged, nothing outstanding unpushed.
329 tests, pyflakes clean. CI: py3.9 + 3.12, compileall, pyflakes, pytest,
help-page render, wheel build + entry-point check.

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

Nothing has ever been verified against the live API by tests — all 329 use a
fake translator. Real-world confidence comes only from actual runs.

## Open items

### 1. Resume prompt fires mid-run (bug, agreed fix: option C)

Translating 3 seasons, the resume prompt appeared during season 2.

Cause: `_resume_choice` in `translate.py` latches only when `_ask_resume` is
actually *called*, and it is only called when `cached_hits` is non-empty.
On a fresh series season 1 has an empty cache, so it never asks and never
latches. Season 1 then writes into the shared cache; season 2 finds its common
lines ("Yes.", "Thank you.") and prompts — mid-run. The message is also false:
it says "translated in an earlier run" when it was this run, minutes ago.

Severity: a long unattended run **stalls waiting for a keypress**.

Reproduced: two seasons under one show, season 1 → 0 prompts, season 2 → 1
prompt (`cached: 3, missing: 1`).

Agreed fix (**C**): snapshot the cache keys the first time the file is opened in
the process, and only prompt about hits inside that snapshot. Makes "cached from
an earlier run" literally true and cannot fire mid-run. Regression test: two
seasons sharing a cache — zero prompts on a fresh series, exactly one on a real
re-run.

Workaround today: `RESUME_PROMPT: false`.

### 2. "not written because 0 unique line(s) remain untranslated" (bug)

Nonsense warning on files whose every style is in the passthrough list
(`→ 0 cues`).

Cause, both in `batch.py`:
- line 147 `if not required or not required.issubset(available): continue` —
  a file with no translatable cues has an empty `required`, so it is skipped and
  never emitted.
- line 175 `if missing_keys and len(missing_keys) <= tolerance:` — an empty set
  is falsy, so it falls through to the failure branch, which reports
  `len(missing_keys)` = 0.

Proposed: write the file if it has any cues at all, so passthrough content is
preserved; report "nothing to translate (all styles passthrough)" as info, not a
warning. Cosmetic — no data lost.

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

### 4. RETRY_ATTEMPTS warning is mis-levelled (cosmetic)

`effective_attempts()` already corrects the condition unconditionally, so the
warning demands action where none exists, on every command. Should be
`log.info`/`detail`, not `log.warning`.

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
