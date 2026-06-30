"""Update flow: git pull + settings merge.

Usage:
    btcli update          — pull latest + report new settings
    btcli update --merge  — pull latest + add new settings to user config
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .config import _INSTALL_DIR, _DEFAULT_SETTINGS_FILE, _find_settings_file
from .logger import log


def _load_json_with_comments(path: Path) -> dict:
    """Load a JSON file that may contain // comments."""
    raw = path.read_text(encoding="utf-8")
    cleaned = re.sub(r'(?m)^\s*//.*$', '', raw)
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
    return json.loads(cleaned)


def run_update() -> None:
    """Run the update flow.

    1. git pull in install dir
    2. If no user settings.conf exists, copy from settings.default.conf
    3. Compare keys and add any new settings to user config
    """
    log.sep()
    log.phase("UPDATE")

    # Step 1: git pull
    log.info("Pulling latest...")
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=str(_INSTALL_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            if "Already up to date" in output:
                log.info("  Already up to date.")
            else:
                log.success(f"  {output}")
        else:
            log.error(f"  git pull failed: {result.stderr.strip()}")
            return
    except Exception as e:
        log.error(f"  git pull failed: {e}")
        return

    # Step 2: Ensure user settings.conf exists (with comments)
    user_file = _INSTALL_DIR / "settings.conf"

    if not _DEFAULT_SETTINGS_FILE.exists():
        log.warning("settings.default.conf not found — skipping settings check")
        return

    if not user_file.exists():
        # First time — copy defaults (with comments) as user config
        import shutil
        shutil.copy2(str(_DEFAULT_SETTINGS_FILE), str(user_file))
        log.success(f"  Created {user_file} from defaults")
        return

    # Step 3: Check for new settings and merge them in
    try:
        default_settings = _load_json_with_comments(_DEFAULT_SETTINGS_FILE)
    except Exception as e:
        log.error(f"  Failed to parse settings.default.conf: {e}")
        return

    try:
        user_settings = _load_json_with_comments(user_file)
    except Exception:
        user_settings = {}

    # Find new keys
    new_keys = set(default_settings.keys()) - set(user_settings.keys())
    removed_keys = set(user_settings.keys()) - set(default_settings.keys())

    if not new_keys and not removed_keys:
        log.success("Settings are up to date.")
        return

    if new_keys:
        log.info(f"  Adding {len(new_keys)} new setting(s):")
        # Read raw content to preserve comments
        raw = user_file.read_text(encoding="utf-8")

        # Build new entries
        new_entries = []
        for key in sorted(new_keys):
            val = json.dumps(default_settings[key], ensure_ascii=False)
            new_entries.append(f'  "{key}": {val}')
            log.item(f"  {key}: {val}")

        # Insert before last }
        insert_text = ",\n" + ",\n".join(new_entries)
        last_brace = raw.rfind("}")
        if last_brace > 0:
            raw = raw[:last_brace] + insert_text + "\n" + raw[last_brace:]
            user_file.write_text(raw, encoding="utf-8")
            log.success(f"  Merged into {user_file}")
        else:
            log.error("  Could not find closing } in user config")

    if removed_keys:
        log.info(f"  Deprecated settings in your config: {', '.join(sorted(removed_keys))}")
