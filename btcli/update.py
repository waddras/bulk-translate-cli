"""Update flow: fetch the latest code, then merge any new settings keys.

Usage:
    btcli update                  — update the current branch, merge new settings
    btcli update --check          — report whether an update is available, change nothing
    btcli update --branch NAME    — switch to NAME and update it
    btcli update --stash          — set aside local edits, update, then restore them

The update is deliberately conservative: it fast-forwards only. If the local
branch has diverged from the remote, it stops and says so rather than creating
a merge commit inside the user's install.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .config import _INSTALL_DIR, _DEFAULT_SETTINGS_FILE
from .logger import log


# ── git plumbing ──────────────────────────────────────────────────────────────

class UpdateError(Exception):
    """Raised when the update cannot proceed. The message is user-facing."""


def _git(repo: Path, *args: str, timeout: int = 120) -> str:
    """Run a git command in *repo* and return stdout, or raise UpdateError."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise UpdateError("git is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise UpdateError(f"git {args[0]} timed out after {timeout}s.")
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or "no output")
        raise UpdateError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def find_repo() -> Path:
    """Locate the git checkout holding this install.

    Prefers the directory this module was loaded from, so an install that
    lives somewhere other than /opt/btcli still updates itself. Falls back to
    the configured install dir.
    """
    candidates = [Path(__file__).resolve().parent.parent, _INSTALL_DIR]
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    raise UpdateError(
        f"{candidates[0]} is not a git checkout, so there is nothing to pull.\n"
        f"  If you installed with pip:  pip install --upgrade bulk-translate-cli\n"
        f"  For a self-updating copy:   git clone <repo-url> {_INSTALL_DIR}"
    )


def current_branch(repo: Path) -> str | None:
    """Return the checked-out branch, or None when HEAD is detached."""
    name = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return None if name == "HEAD" else name


def local_changes(repo: Path) -> list[str]:
    """Return tracked files that differ from HEAD, staged or not.

    Untracked files are ignored: settings.conf and the runtime state files are
    gitignored anyway, and an untracked file never blocks a fast-forward.

    Uses ``diff --name-only`` rather than ``status --porcelain`` so the result
    is a plain list of paths with no status prefix to strip.
    """
    diff = _git(repo, "diff", "--name-only", "HEAD")
    return [line for line in diff.splitlines() if line.strip()]


def _remote_exists(repo: Path, ref: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", ref)
        return True
    except UpdateError:
        return False


def remote_branches(repo: Path) -> list[str]:
    """Branch names that exist on origin, asked of the remote directly.

    Not read from refs/remotes, which only lists what this clone happens to
    track — a --single-branch clone tracks just one.
    """
    listing = _git(repo, "ls-remote", "--heads", "origin")
    names = []
    for line in listing.splitlines():
        parts = line.split("refs/heads/", 1)
        if len(parts) == 2:
            names.append(parts[1].strip())
    return names


STANDARD_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def widen_fetch_refspec(repo: Path) -> bool:
    """Make origin fetch every branch, as a normal clone does.

    A --single-branch or --depth 1 clone is configured to fetch one branch
    only. That breaks more than the fetch: git will not set up tracking for,
    or even guess the name of, a branch its refspec does not cover. Widening
    the refspec once restores ordinary behaviour, including plain `git pull`.

    Returns True when the config was changed.
    """
    try:
        current = _git(repo, "config", "--get-all", "remote.origin.fetch")
    except UpdateError:
        current = ""
    if STANDARD_REFSPEC in current.splitlines():
        return False
    _git(repo, "config", "--replace-all", "remote.origin.fetch", STANDARD_REFSPEC)
    return True


def _fetch_branch(repo: Path, target: str) -> None:
    """Fetch *target* into refs/remotes/origin/<target>."""
    if widen_fetch_refspec(repo):
        log.info("  Configured origin to fetch all branches.")
    refspec = f"+refs/heads/{target}:refs/remotes/origin/{target}"
    try:
        _git(repo, "fetch", "origin", refspec, "--prune")
    except UpdateError as e:
        available = []
        try:
            available = remote_branches(repo)
        except UpdateError:
            pass
        if available and target not in available:
            listing = "\n".join(f"    {name}" for name in sorted(available)[:20])
            raise UpdateError(
                f"origin has no branch named '{target}'. Available:\n{listing}"
            )
        raise UpdateError(f"Could not reach origin: {e}")


# ── settings merge ────────────────────────────────────────────────────────────

def _load_json_with_comments(path: Path) -> dict:
    """Load a JSON file that may contain // comments and trailing commas."""
    raw = path.read_text(encoding="utf-8")
    cleaned = re.sub(r'(?m)^\s*//.*$', '', raw)
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
    return json.loads(cleaned)


def _key_line_span(lines: list, key: str) -> list:
    """Line indexes belonging to every declaration of *key*, comments excluded.

    Works on the raw text rather than a parsed dict so the rest of the file —
    comments, ordering, formatting — survives untouched. A value may span
    several lines, so each declaration runs until brackets balance and the entry
    is closed.
    """
    spans = []
    opener = re.compile(r'^\s*"' + re.escape(key) + r'"\s*:')

    index = 0
    while index < len(lines):
        if not opener.match(lines[index]):
            index += 1
            continue

        start = index
        depth = 0
        in_string = False
        escaped = False
        while index < len(lines):
            for char in lines[index]:
                if escaped:
                    escaped = False
                    continue
                if char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = not in_string
                elif not in_string:
                    if char in "[{":
                        depth += 1
                    elif char in "]}":
                        depth -= 1
            index += 1
            if depth <= 0:
                break
        spans.append((start, index))

    return spans


def reset_settings(user_file: Path, default_file: Path, keys: list,
                   apply: bool = True) -> dict:
    """Restore the shipped default for each of *keys*, in place.

    Rewrites only those entries and leaves the rest of the file — including
    comments — alone. A key declared more than once collapses to a single
    entry, which is the point: JSON silently keeps the last of a repeated key,
    so the file can hold two values for one setting.

    Returns a summary with 'reset', 'unknown', 'absent' and 'collapsed'.
    """
    summary: dict = {"reset": {}, "unknown": [], "absent": [], "collapsed": []}

    if not default_file.exists():
        raise UpdateError(f"{default_file.name} not found — no defaults to reset to.")
    if not user_file.exists():
        raise UpdateError(f"{user_file} does not exist yet; run btcli update first.")

    defaults = _load_json_with_comments(default_file)
    raw = user_file.read_text(encoding="utf-8")
    lines = raw.splitlines(keepends=True)

    # Resolve every key first, so an unknown name changes nothing.
    targets = {}
    for key in keys:
        if key not in defaults:
            summary["unknown"].append(key)
            continue
        spans = _key_line_span(lines, key)
        if not spans:
            summary["absent"].append(key)
            continue
        if len(spans) > 1:
            summary["collapsed"].append(key)
        targets[key] = spans
        summary["reset"][key] = defaults[key]

    if summary["unknown"]:
        raise UpdateError(
            f"Not a known setting: {', '.join(summary['unknown'])}\n"
            f"  Names are case-sensitive; see {default_file.name} for the full list."
        )
    if not targets or not apply:
        return summary

    # Rewrite from the bottom up so earlier line numbers stay valid.
    replacements = []
    for key, spans in targets.items():
        value = json.dumps(defaults[key], ensure_ascii=False)
        for position, (start, end) in enumerate(spans):
            # Only the first declaration keeps a line; the rest are dropped.
            text = f'  "{key}": {value},\n' if position == 0 else None
            replacements.append((start, end, text))

    for start, end, text in sorted(replacements, key=lambda item: -item[0]):
        trailing = lines[end - 1].rstrip()
        had_comma = trailing.endswith(",")
        if text is None:
            lines[start:end] = []
        else:
            if not had_comma:
                text = text.rstrip("\n").rstrip(",") + "\n"
            lines[start:end] = [text]

    updated = "".join(lines)
    # A dropped duplicate can leave the final entry with a trailing comma.
    updated = re.sub(r',(\s*})', r'\1', updated)

    # Never write something we cannot read back.
    try:
        _load_json_with_comments_text(updated)
    except Exception as e:
        raise UpdateError(
            f"Resetting would have produced an unreadable {user_file.name} ({e}); "
            f"nothing was written."
        )

    backup = user_file.with_suffix(user_file.suffix + ".bak")
    backup.write_text(raw, encoding="utf-8")
    user_file.write_text(updated, encoding="utf-8")
    summary["backup"] = backup
    return summary


def dedupe_settings(user_file: Path, apply: bool = True) -> dict:
    """Remove repeated declarations of a setting, keeping the one in effect.

    JSON keeps the last of a repeated key, so the last declaration is the value
    already being used. Keeping it means the file starts saying what btcli was
    doing all along — nothing changes behaviour, the config just stops lying.

    Distinct from --reset, which restores the shipped default and so may change
    a value you chose deliberately.
    """
    summary: dict = {"removed": {}, "backup": None}

    if not user_file.exists():
        raise UpdateError(f"{user_file} does not exist.")

    raw = user_file.read_text(encoding="utf-8")
    from .validate import find_duplicate_keys
    duplicates = find_duplicate_keys(raw)
    if not duplicates:
        return summary

    lines = raw.splitlines(keepends=True)
    kept = _load_json_with_comments_text(raw)

    drop: list = []
    for key in duplicates:
        spans = _key_line_span(lines, key)
        # Keep the final declaration; it is the one JSON already honours.
        for start, end in spans[:-1]:
            drop.append((start, end))
        summary["removed"][key] = {"dropped": len(spans) - 1, "kept": kept.get(key)}

    if not apply:
        return summary

    for start, end in sorted(drop, key=lambda item: -item[0]):
        lines[start:end] = []

    updated = "".join(lines)
    try:
        reparsed = _load_json_with_comments_text(updated)
    except Exception as e:
        raise UpdateError(
            f"Removing duplicates would have produced an unreadable "
            f"{user_file.name} ({e}); nothing was written.")

    # The effective configuration must be identical, or this did more than dedupe.
    if reparsed != kept:
        raise UpdateError(
            f"Removing duplicates would have changed the effective settings; "
            f"nothing was written. Edit {user_file.name} by hand.")

    backup = user_file.with_suffix(user_file.suffix + ".bak")
    backup.write_text(raw, encoding="utf-8")
    user_file.write_text(updated, encoding="utf-8")
    summary["backup"] = backup
    return summary


def _load_json_with_comments_text(raw: str) -> dict:
    """Parse settings text that may contain // comments and trailing commas."""
    cleaned = re.sub(r'(?m)^\s*//.*$', '', raw)
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
    return json.loads(cleaned)


def merge_settings(user_file: Path, default_file: Path, apply: bool = True) -> dict:
    """Add settings present in defaults but missing from the user config.

    Returns a summary dict with 'created', 'added' and 'deprecated'. Existing
    user values are never touched, and comments in the file are preserved by
    appending raw text rather than re-serialising.
    """
    summary: dict = {"created": False, "added": {}, "deprecated": []}

    if not default_file.exists():
        raise UpdateError(
            f"{default_file.name} not found — cannot tell which settings are new."
        )

    if not user_file.exists():
        if apply:
            shutil.copy2(str(default_file), str(user_file))
        summary["created"] = True
        return summary

    default_settings = _load_json_with_comments(default_file)
    try:
        user_settings = _load_json_with_comments(user_file)
    except Exception as e:
        raise UpdateError(
            f"Could not parse {user_file}: {e}\n"
            f"  Fix the syntax, or move it aside and re-run to get a fresh copy."
        )

    new_keys = sorted(set(default_settings) - set(user_settings))
    summary["added"] = {k: default_settings[k] for k in new_keys}
    summary["deprecated"] = sorted(set(user_settings) - set(default_settings))

    if new_keys and apply:
        raw = user_file.read_text(encoding="utf-8")
        last_brace = raw.rfind("}")
        if last_brace < 0:
            raise UpdateError(f"No closing brace found in {user_file}.")
        entries = [
            f'  "{k}": {json.dumps(default_settings[k], ensure_ascii=False)}'
            for k in new_keys
        ]
        head = raw[:last_brace].rstrip()
        # A config may legitimately end "…, }" or "{ }". Normalise to exactly
        # one separator so the result parses either way.
        if head.endswith(","):
            head = head[:-1].rstrip()
        separator = "" if head.endswith("{") else ","
        raw = head + separator + "\n" + ",\n".join(entries) + "\n" + raw[last_brace:]
        user_file.write_text(raw, encoding="utf-8")

    return summary


# ── flow ──────────────────────────────────────────────────────────────────────

def run_update(branch: str | None = None, check: bool = False,
               stash: bool = False, reset: list | None = None,
               dedupe: bool = False) -> None:
    """Run the update flow. See module docstring for the argument meanings."""
    if reset or dedupe:
        log.sep()
        log.phase("RESET SETTINGS" if reset else "DEDUPE SETTINGS")
        try:
            if reset:
                _run_reset(reset)
            else:
                _run_dedupe()
        except UpdateError as e:
            log.error(str(e))
        return

    log.sep()
    log.phase("UPDATE — checking for a newer version" if check else "UPDATE")

    try:
        _run_update(branch=branch, check=check, stash=stash)
    except UpdateError as e:
        log.error(str(e))


def _run_reset(keys: list) -> None:
    """Restore the shipped defaults for the named settings."""
    repo = find_repo()
    user_file = _settings_path(repo)
    summary = reset_settings(user_file, _default_path(repo), keys)

    if summary["absent"]:
        log.info(f"Already using the default (not set in your config): "
                 f"{', '.join(summary['absent'])}")
    if not summary["reset"]:
        log.success("Nothing to change.")
        return

    for key in summary["collapsed"]:
        log.warning(f"{key} was declared more than once; collapsed to a single entry.")

    log.success(f"Reset {len(summary['reset'])} setting(s) in {user_file.name}:")
    for key, value in summary["reset"].items():
        rendered = json.dumps(value, ensure_ascii=False)
        if len(rendered) > 70:
            rendered = rendered[:67] + "..."
        log.item(f"{key}: {rendered}")
    log.detail(f"  Previous version saved as {summary['backup'].name}")


def _run_dedupe() -> None:
    """Collapse repeated settings, keeping the value already in effect."""
    repo = find_repo()
    user_file = _settings_path(repo)
    summary = dedupe_settings(user_file)

    if not summary["removed"]:
        log.success("No setting is declared more than once.")
        return

    log.success(f"Collapsed {len(summary['removed'])} repeated setting(s) in "
                f"{user_file.name}:")
    for key, info in summary["removed"].items():
        rendered = json.dumps(info["kept"], ensure_ascii=False)
        if len(rendered) > 60:
            rendered = rendered[:57] + "..."
        log.item(f"{key}: dropped {info['dropped']} earlier declaration(s), "
                 f"kept {rendered}")
    log.info("  Your effective settings are unchanged; only the duplicates are gone.")
    log.detail(f"  Previous version saved as {summary['backup'].name}")


def _run_update(branch: str | None, check: bool, stash: bool) -> None:
    repo = find_repo()
    log.info(f"Install: {repo}")

    on_branch = current_branch(repo)
    target = branch or on_branch
    if target is None:
        raise UpdateError(
            "HEAD is detached, so there is no branch to update.\n"
            "  Pick one explicitly:  btcli update --branch main"
        )

    log.info(f"Fetching origin/{target}...")
    _fetch_branch(repo, target)

    remote_ref = f"origin/{target}"
    if not _remote_exists(repo, remote_ref):
        raise UpdateError(f"origin/{target} could not be fetched.")

    # How far behind are we?
    behind = _git(repo, "rev-list", "--count", f"HEAD..{remote_ref}")
    ahead = _git(repo, "rev-list", "--count", f"{remote_ref}..HEAD")
    switching = target != on_branch

    if check:
        _report_check(target, switching, behind, ahead, repo)
        return

    if not switching and behind == "0":
        log.success(f"Already up to date with {remote_ref}.")
        _merge_and_report(repo)
        return

    if ahead != "0" and not switching:
        raise UpdateError(
            f"Your checkout has {ahead} local commit(s) that {remote_ref} does not.\n"
            f"  btcli only fast-forwards, so it will not rewrite them.\n"
            f"  Resolve manually:  cd {repo} && git rebase {remote_ref}"
        )

    # Local edits would be clobbered by checkout/merge.
    dirty = local_changes(repo)
    stashed = False
    if dirty:
        if not stash:
            listing = "\n".join(f"    {f}" for f in dirty[:10])
            more = f"\n    ... and {len(dirty) - 10} more" if len(dirty) > 10 else ""
            raise UpdateError(
                f"{len(dirty)} tracked file(s) have uncommitted edits:\n"
                f"{listing}{more}\n"
                f"  Set them aside and update:  btcli update --stash\n"
                f"  Or discard them:            cd {repo} && git checkout -- ."
            )
        log.info(f"Stashing {len(dirty)} modified file(s)...")
        _git(repo, "stash", "push", "--message", "btcli update")
        stashed = True

    try:
        if switching:
            log.info(f"Switching to {target}...")
            _checkout(repo, target)
        log.info(f"Fast-forwarding to {remote_ref}...")
        before = _git(repo, "rev-parse", "--short", "HEAD")
        _git(repo, "merge", "--ff-only", remote_ref)
        after = _git(repo, "rev-parse", "--short", "HEAD")
    except UpdateError:
        if stashed:
            log.warning("Update failed — restoring your stashed edits.")
            _restore_stash(repo)
        raise

    if before == after:
        log.success(f"Already up to date with {remote_ref}.")
    else:
        changed = _git(repo, "diff", "--name-only", f"{before}..{after}")
        count = len([line for line in changed.splitlines() if line.strip()])
        log.success(f"Updated {before} → {after} ({count} file(s) changed)")
        for line in _git(repo, "log", "--oneline", f"{before}..{after}").splitlines()[:10]:
            log.item(line)

    if stashed:
        log.info("Restoring your edits...")
        _restore_stash(repo)

    _merge_and_report(repo)


def _checkout(repo: Path, target: str) -> None:
    """Check out *target*, creating it from origin if it is not local yet.

    A plain ``git checkout <name>`` can guess a remote branch, but only when
    the remote's fetch refspec covers it — which is not true in a
    --single-branch clone. Creating the branch explicitly works either way.
    Never uses -B, so an existing local branch keeps its commits.
    """
    if _remote_exists(repo, f"refs/heads/{target}"):
        _git(repo, "checkout", target)
    else:
        _git(repo, "checkout", "-b", target, "--track", f"origin/{target}")


def _restore_stash(repo: Path) -> None:
    """Pop the stash created by this run, reporting conflicts clearly."""
    try:
        _git(repo, "stash", "pop")
        log.success("  Local edits restored.")
    except UpdateError as e:
        log.warning(
            f"  Could not restore automatically: {e}\n"
            f"  Your edits are safe in the stash:  cd {repo} && git stash list"
        )


def _report_check(target: str, switching: bool, behind: str, ahead: str,
                  repo: Path) -> None:
    """Print what an update would do, without doing it."""
    if switching:
        log.info(f"Would switch to {target} and update it.")
    elif behind == "0":
        log.success(f"Up to date with origin/{target}.")
    else:
        log.warning(f"{behind} new commit(s) available on origin/{target}:")
        for line in _git(repo, "log", "--oneline", f"HEAD..origin/{target}").splitlines()[:10]:
            log.item(line)

    if ahead != "0":
        log.warning(f"You also have {ahead} local commit(s) not on origin/{target}.")

    dirty = local_changes(repo)
    if dirty:
        log.warning(f"{len(dirty)} tracked file(s) have uncommitted edits "
                    f"(use --stash to update anyway).")

    try:
        summary = merge_settings(_settings_path(repo), _default_path(repo), apply=False)
    except UpdateError as e:
        log.warning(str(e))
        return
    if summary["created"]:
        log.info("Would create settings.conf from the shipped defaults.")
    elif summary["added"]:
        log.info(f"Would add {len(summary['added'])} new setting(s): "
                 f"{', '.join(summary['added'])}")


def _settings_path(repo: Path) -> Path:
    return repo / "settings.conf"


def _default_path(repo: Path) -> Path:
    shipped = repo / "settings.default.conf"
    return shipped if shipped.exists() else _DEFAULT_SETTINGS_FILE


def _merge_and_report(repo: Path) -> None:
    """Merge new settings keys into the user config and report what changed."""
    user_file = _settings_path(repo)
    try:
        summary = merge_settings(user_file, _default_path(repo), apply=True)
    except UpdateError as e:
        log.warning(str(e))
        return

    if summary["created"]:
        log.success(f"Created {user_file} from the shipped defaults.")
        return

    if summary["added"]:
        log.info(f"Added {len(summary['added'])} new setting(s) to {user_file.name}:")
        for key, val in summary["added"].items():
            log.item(f"{key}: {json.dumps(val, ensure_ascii=False)}")
    else:
        log.success("Settings are up to date.")

    if summary["deprecated"]:
        log.info("No longer used by btcli (safe to delete): "
                 + ", ".join(summary["deprecated"]))
