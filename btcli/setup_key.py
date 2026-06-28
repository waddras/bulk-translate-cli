"""First-use API key setup prompt."""
from __future__ import annotations

import os
from pathlib import Path

from .config import cfg


def check_api_key() -> str | None:
    """Check if API key is available. If not, prompt user.

    Returns the API key if found/entered, or None if user declines.
    """
    key = cfg.get("GEMINI_API_KEY", "")
    if key:
        return key

    key_file = Path(os.path.expanduser(cfg.get("GEMINI_API_KEY_FILE", "~/.btcli.env")))

    print("\nNo API key found.")
    answer = input("Do you want to enter your API key? [Y/n]: ").strip().lower()

    if answer in ("", "y", "yes"):
        if key_file.exists():
            print(f"\nKey file exists at: {key_file}")
            print(f"Edit it and add your key:")
            print(f"  nano {key_file}")
            return None
        else:
            create = input(f"Create {key_file}? [Y/n]: ").strip().lower()
            if create in ("", "y", "yes"):
                api_key = input("Enter your Gemini API key: ").strip()
                if api_key:
                    key_file.parent.mkdir(parents=True, exist_ok=True)
                    key_file.write_text(f"GEMINI_API_KEY={api_key}\n")
                    os.chmod(str(key_file), 0o600)
                    print(f"Saved to {key_file}")
                    cfg["GEMINI_API_KEY"] = api_key
                    return api_key
                else:
                    print("No key entered.")
                    return None
            else:
                _print_instructions(key_file)
                return None
    else:
        _print_instructions(key_file)
        return None


def _print_instructions(key_file: Path):
    """Print manual setup instructions."""
    print(f"\nSetup instructions:")
    print(f"  echo 'GEMINI_API_KEY=your-key-here' > {key_file}")
    print(f"  chmod 600 {key_file}")
    print(f"\nOr set environment variable:")
    print(f"  export GEMINI_API_KEY=\"your-key-here\"")
