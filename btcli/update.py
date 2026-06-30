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


def run_update(merge: bool = False) -> None:
    """Run the update flow.

    1. git pull in install dir
    2. Compare settings.default.conf keys vs user settings.conf
    3. Report new/removed settings
    4. If --merge: add new settings to user config with defaults
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

    # Step 2: Compare settings
    if not _DEFAULT_SETTINGS_FILE.exists():
        log.warning("settings.default.conf not found — skipping settings check")
        return

    try:
        default_settings = _load_json_with_comments(_DEFAULT_SETTINGS_FILE)
    except Exception as e:
        log.error(f"  Failed to parse settings.default.conf: {e}")
        return

    # Find user settings file
    user_file = _find_settings_file()
    user_settings = {}
    if user_file and user_file.exists():
        try:
            user_settings = _load_json_with_comments(user_file)
        except Exception:
            user_settings = {}

    # Find new keys (in default but not in user)
    default_keys = set(default_settings.keys())
    user_keys = set(user_settings.keys())
    new_keys = default_keys - user_keys
    removed_keys = user_keys - default_keys

    log.sep()
    if not new_keys and not removed_keys:
        log.success("Settings are up to date — no new settings.")
        return

    if new_keys:
        log.info(f"  New settings available ({len(new_keys)}):")
        for key in sorted(new_keys):
            val = default_settings[key]
            display = json.dumps(val) if not isinstance(val, str) else f'"{val}"'
            log.item(f"  {key}: {display}")

    if removed_keys:
        log.info(f"\n  Deprecated settings in your config ({len(removed_keys)}):")
        for key in sorted(removed_keys):
            log.item(f"  {key}")

    # Step 3: Merge if requested
    if merge and new_keys:
        if not user_file:
            # Copy settings.default.conf as user's starting point (preserves comments)
            user_file = _INSTALL_DIR / "settings.conf"
            if _DEFAULT_SETTINGS_FILE.exists():
                import shutil
                shutil.copy2(str(_DEFAULT_SETTINGS_FILE), str(user_file))
                log.success(f"  Created {user_file} from defaults (with comments)")
            else:
                # Fallback: write JSON
                for key in sorted(new_keys):
                    user_settings[key] = default_settings[key]
                user_file.parent.mkdir(parents=True, exist_ok=True)
                with open(user_file, "w", encoding="utf-8") as f:
                    json.dump(user_settings, f, indent=2, ensure_ascii=False)
                log.success(f"  Created {user_file}")
        else:
            # User file exists — add new keys with default values
            # Read raw content to preserve comments
            raw = user_file.read_text(encoding="utf-8")

            # Build new entries to add before the closing }
            new_entries = []
            for key in sorted(new_keys):
                val = json.dumps(default_settings[key], ensure_ascii=False)
                new_entries.append(f'  "{key}": {val}')

            # Insert before last }
            insert_text = ",\n" + ",\n".join(new_entries)
            last_brace = raw.rfind("}")
            if last_brace > 0:
                raw = raw[:last_brace] + insert_text + "\n" + raw[last_brace:]
                user_file.write_text(raw, encoding="utf-8")
                log.success(f"  Added {len(new_keys)} new setting(s) to {user_file}")
            else:
                log.error("  Could not find closing } in user config")

    elif new_keys and not merge:
        log.info(f"\n  Run 'btcli update --merge' to add these to your config.")
