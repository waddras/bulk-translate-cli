"""Shared terminal prompt helpers.

Used by interactive mode and by the end-of-job retry offer. Everything is
printed on stdout so prompts and headings stay in order, and invalid input is
re-asked rather than raising.
"""
from __future__ import annotations

import sys


class Abort(Exception):
    """Raised when the user cancels a prompt."""


def is_interactive() -> bool:
    """True when there is a real terminal to prompt on."""
    return sys.stdin.isatty()


def ask(question: str, default: str = "") -> str:
    """Ask a free-text question. Empty input returns the default."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        raise Abort("input stream closed")
    return answer or default


def ask_choice(question: str, choices: list, default: str) -> str:
    """Ask until the answer is one of choices (case-insensitive)."""
    options = "/".join(choices)
    while True:
        answer = ask(f"{question} ({options})", default).lower()
        if answer in choices:
            return answer
        print(f"  Please enter one of: {options}")


def ask_yes_no(question: str, default: bool = True) -> bool:
    """Ask a yes/no question. Enter accepts the default."""
    label = "Y/n" if default else "y/N"
    while True:
        answer = ask(question, label).lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if answer == label.lower():
            return default
        print("  Please enter y or n")


def ask_optional_int(question: str, default_label: str, minimum: int = 1):
    """Ask for a positive integer, or nothing to keep the default."""
    while True:
        answer = ask(question, default_label)
        if answer == default_label:
            return None
        if answer.isdigit() and int(answer) >= minimum:
            return int(answer)
        print(f"  Enter a number >= {minimum}, or press Enter for {default_label}")


def columns(entries: list, per_row: int = 3, width: int = 26) -> None:
    """Print numbered entries in aligned columns."""
    for start in range(0, len(entries), per_row):
        row = entries[start:start + per_row]
        print("  " + "".join(cell.ljust(width) for cell in row).rstrip())


def header(title: str) -> None:
    """Section heading on stdout, so prompts and headings stay in order."""
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)
