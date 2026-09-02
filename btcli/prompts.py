"""Shared terminal prompt helpers.

Used by interactive mode and by the end-of-job missing-lines offer. Everything
is printed on stdout so prompts and headings stay in order, and invalid input is
re-asked rather than raising.

Colour is applied with plain ANSI codes rather than rich, because the logger owns
a rich console on stderr; mixing two writers reorders output. Colour is disabled
automatically when stdout is not a terminal, when NO_COLOR is set, or when
TERM=dumb, so redirected output and logs stay clean.
"""
from __future__ import annotations

import os
import sys


class Abort(Exception):
    """Raised when the user cancels a prompt."""


# ── Colour ────────────────────────────────────────────────────────────────────

HEADING = "96;1"    # bright cyan: section headings
QUESTION = "93;1"   # bright yellow: the question being asked
DEFAULT = "90"      # grey: the default value in brackets
HINT = "90"         # grey: examples and explanatory text
ITEM = "96"         # cyan: list numbers and keys
GOOD = "92;1"       # bright green: confirmations
BAD = "91;1"        # bright red: invalid input
WARN = "93"         # yellow: cautions


def colour_enabled() -> bool:
    """True when it is safe to emit ANSI colour on stdout."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    return sys.stdout.isatty()


def paint(text: str, code: str) -> str:
    """Wrap text in an ANSI colour, or return it unchanged when disabled."""
    if not code or not colour_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def is_interactive() -> bool:
    """True when there is a real terminal to prompt on."""
    return sys.stdin.isatty()


# ── Output helpers ────────────────────────────────────────────────────────────

def header(title: str) -> None:
    """Section heading on stdout, so prompts and headings stay in order."""
    rule = "=" * 60
    print("\n" + paint(rule, HEADING))
    print(paint(title, HEADING))
    print(paint(rule, HEADING))


def hint(text: str) -> None:
    """Explanatory line, dimmed so it does not compete with the question."""
    print(paint(text, HINT))


def note(text: str) -> None:
    """Neutral informational line."""
    print(text)


def good(text: str) -> None:
    print(paint(text, GOOD))


def warn(text: str) -> None:
    print(paint(text, WARN))


def bad(text: str) -> None:
    """Invalid-input feedback."""
    print(paint(text, BAD))


def columns(entries: list, per_row: int = 3, width: int = 26,
            code: str = ITEM) -> None:
    """Print numbered entries in aligned columns.

    Padding is measured on the plain text and colour applied afterwards, so
    escape codes never distort the column widths.
    """
    for start in range(0, len(entries), per_row):
        row = entries[start:start + per_row]
        cells = [paint(cell.ljust(width), code) for cell in row]
        print("  " + "".join(cells).rstrip())


# ── Questions ─────────────────────────────────────────────────────────────────

def ask(question: str, default: str = "", code: str = QUESTION) -> str:
    """Ask a free-text question. Empty input returns the default."""
    prompt = paint(question, code)
    if default:
        prompt += paint(f" [{default}]", DEFAULT)
    try:
        answer = input(f"{prompt}: ").strip()
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
        bad(f"  Please enter one of: {options}")


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
        bad("  Please enter y or n")


def ask_optional_int(question: str, default_label: str, minimum: int = 1):
    """Ask for a positive integer, or nothing to keep the default."""
    while True:
        answer = ask(question, default_label)
        if answer == default_label:
            return None
        if answer.isdigit() and int(answer) >= minimum:
            return int(answer)
        bad(f"  Enter a number >= {minimum}, or press Enter for {default_label}")


def ask_menu(question: str, options: list, default: str) -> str:
    """Ask a multiple-choice question.

    options is a list of (key, label) pairs. Returns the chosen key. The default
    key is shown capitalised, and either the key or its full label word is
    accepted.
    """
    print(paint(question, QUESTION))
    keys = []
    for key, label in options:
        key = key.lower()
        keys.append(key)
        shown = key.upper() if key == default.lower() else key
        print("    " + paint(f"[{shown}]", ITEM) + f" {label}")

    while True:
        answer = ask("  Choice", default).lower()
        if answer in keys:
            return answer
        bad(f"  Please enter one of: {', '.join(keys)}")
