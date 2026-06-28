"""Logging system with 3 verbosity levels.

Levels:
  - minimal (--quiet): timestamps + phase summaries only, no progress bar
  - medium (default): rich progress bars, colored output, elapsed time, ETA
  - full (--verbose): all of medium + per-attempt debug info, structured with levels

Usage:
    from .logger import log

    log.phase("PHASE 1 - Building blob...")
    log.info("Found 342 cues")
    log.detail("  Attempt 2/5 - HTTP 429")  # only shown in full mode
    log.success("All lines translated")
    log.warning("3 lines untranslated")
    log.error("API key not set")

    # Progress tracking (medium + full only)
    log.start_progress("Translating", total=7)
    log.advance_progress()  # +1
    log.finish_progress()

    # Elapsed time
    log.start_timer()
    log.elapsed()  # returns "01:23"
"""
from __future__ import annotations

import sys
import time
from enum import Enum
from pathlib import Path

# ── Verbosity Levels ──────────────────────────────────────────────────────────

class Level(Enum):
    MINIMAL = "minimal"
    MEDIUM = "medium"
    FULL = "full"


# ── Logger Class ──────────────────────────────────────────────────────────────

class Logger:
    """Singleton-style logger with 3 verbosity modes."""

    def __init__(self):
        self._level = Level.MEDIUM
        self._start_time: float = 0.0
        self._log_file: Path | None = None
        self._log_fh = None
        self._console = None
        self._progress = None
        self._task = None
        self._rich_available = False

        try:
            from rich.console import Console
            from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn
            self._rich_available = True
            self._console = Console(stderr=True)
        except ImportError:
            self._rich_available = False

    # ── Configuration ─────────────────────────────────────────────────────────

    def set_level(self, level: str):
        """Set verbosity: 'minimal', 'medium', or 'full'."""
        self._level = Level(level)

    def set_log_file(self, path: str):
        """Enable logging to a file (all levels written regardless of verbosity)."""
        self._log_file = Path(path)
        self._log_fh = open(self._log_file, "a", encoding="utf-8")

    @property
    def level(self) -> Level:
        return self._level

    @property
    def is_minimal(self) -> bool:
        return self._level == Level.MINIMAL

    @property
    def is_medium(self) -> bool:
        return self._level == Level.MEDIUM

    @property
    def is_full(self) -> bool:
        return self._level == Level.FULL

    # ── Timer ─────────────────────────────────────────────────────────────────

    def start_timer(self):
        """Start the elapsed timer."""
        self._start_time = time.time()

    def elapsed(self) -> str:
        """Return elapsed time as MM:SS string."""
        if not self._start_time:
            return "00:00"
        secs = int(time.time() - self._start_time)
        mins = secs // 60
        secs = secs % 60
        return f"{mins:02d}:{secs:02d}"

    def elapsed_seconds(self) -> float:
        """Return elapsed seconds as float."""
        if not self._start_time:
            return 0.0
        return time.time() - self._start_time

    # ── Core Output ───────────────────────────────────────────────────────────

    def _write_file(self, msg: str):
        """Write to log file if configured."""
        if self._log_fh:
            ts = time.strftime("%H:%M:%S")
            self._log_fh.write(f"[{ts}] {msg}\n")
            self._log_fh.flush()

    def _print_plain(self, msg: str):
        """Plain print to stdout."""
        print(msg)
        self._write_file(msg)

    def _print_rich(self, msg: str, style: str = ""):
        """Print with rich styling (if available and in medium/full mode)."""
        self._write_file(msg)
        if self._rich_available and self._console and not self.is_minimal:
            self._console.print(msg, style=style, highlight=False)
        else:
            print(msg)

    # ── Log Methods ───────────────────────────────────────────────────────────

    def sep(self):
        """Print separator line."""
        line = "=" * 60
        if self.is_minimal:
            self._write_file(line)
        else:
            self._print_rich(line, style="dim")

    def phase(self, msg: str):
        """Phase header — shown in all modes."""
        ts = f"[{self.elapsed()}] " if self._start_time else ""
        full_msg = f"{ts}{msg}"
        if self.is_minimal:
            self._print_plain(full_msg)
        else:
            self._print_rich(full_msg, style="bold cyan")

    def info(self, msg: str):
        """Info message — shown in medium and full."""
        if self.is_minimal:
            self._write_file(msg)
            return
        self._print_rich(msg)

    def detail(self, msg: str):
        """Detail/debug message — shown only in full mode."""
        self._write_file(msg)
        if self.is_full:
            self._print_rich(msg, style="dim")

    def success(self, msg: str):
        """Success message — shown in all modes."""
        ts = f"[{self.elapsed()}] " if self._start_time else ""
        full_msg = f"{ts}{msg}"
        if self.is_minimal:
            self._print_plain(full_msg)
        else:
            self._print_rich(full_msg, style="bold green")

    def warning(self, msg: str):
        """Warning — shown in all modes."""
        full_msg = f"WARNING: {msg}"
        if self.is_minimal:
            self._print_plain(full_msg)
        else:
            self._print_rich(full_msg, style="bold yellow")

    def error(self, msg: str):
        """Error — shown in all modes."""
        full_msg = f"ERROR: {msg}"
        if self.is_minimal:
            self._print_plain(full_msg)
        else:
            self._print_rich(full_msg, style="bold red")

    def stat(self, label: str, value: str):
        """Key-value stat line — shown in medium and full."""
        if self.is_minimal:
            self._write_file(f"  {label}: {value}")
            return
        self._print_rich(f"  [bold]{label}:[/bold] {value}")

    def item(self, msg: str):
        """List item — shown in medium and full."""
        if self.is_minimal:
            self._write_file(f"  {msg}")
            return
        self._print_rich(f"  {msg}")

    def chunk_status(self, chunk_num: int, total: int, lines: int, tokens: int, model: str):
        """Chunk translation status — shown in medium (compact) and full (detailed)."""
        msg = f"  CHUNK {chunk_num}/{total} - {lines} lines, ~{tokens} tokens - model: {model}"
        if self.is_minimal:
            self._write_file(msg)
        elif self.is_full:
            self._print_rich(msg, style="blue")
        else:
            # Medium: compact inline
            self._print_rich(msg, style="blue")

    def chunk_success(self, chunk_num: int, keys: int):
        """Chunk succeeded."""
        msg = f"    SUCCESS ({keys} keys)"
        if self.is_minimal:
            self._write_file(msg)
        else:
            self._print_rich(msg, style="green")

    def chunk_fail(self, chunk_num: int, reason: str):
        """Chunk failed."""
        msg = f"    FAILED: {reason}"
        if self.is_minimal:
            self._write_file(msg)
        else:
            self._print_rich(msg, style="red")

    def attempt(self, attempt_num: int, max_attempts: int, msg: str):
        """Per-attempt detail — only in full mode."""
        full_msg = f"    Attempt {attempt_num}/{max_attempts} - {msg}"
        self._write_file(full_msg)
        if self.is_full:
            self._print_rich(full_msg, style="dim yellow")

    def cooldown(self, seconds: float):
        """Cooldown notice."""
        msg = f"  Cooldown: waiting {seconds:.0f}s..."
        if self.is_minimal:
            self._write_file(msg)
        elif self.is_full:
            self._print_rich(msg, style="dim")
        else:
            # Medium: show but dim
            self._print_rich(msg, style="dim")

    # ── Progress Bar (medium + full) ──────────────────────────────────────────

    def start_progress(self, description: str, total: int):
        """Start a progress bar (medium and full modes only)."""
        if self.is_minimal or not self._rich_available:
            self._write_file(f"Starting: {description} ({total} items)")
            return

        from rich.progress import (
            Progress, SpinnerColumn, BarColumn, TextColumn,
            TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn,
        )

        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=self._console,
        )
        self._progress.start()
        self._task = self._progress.add_task(description, total=total)

    def advance_progress(self, advance: int = 1):
        """Advance progress bar by N steps."""
        if self._progress and self._task is not None:
            self._progress.advance(self._task, advance=advance)

    def update_progress(self, description: str = None, completed: int = None):
        """Update progress description or completed count."""
        if self._progress and self._task is not None:
            kwargs = {}
            if description:
                kwargs["description"] = description
            if completed is not None:
                kwargs["completed"] = completed
            self._progress.update(self._task, **kwargs)

    def finish_progress(self):
        """Complete and remove progress bar."""
        if self._progress:
            self._progress.stop()
            self._progress = None
            self._task = None

    # ── Summary Table (medium + full) ─────────────────────────────────────────

    def summary(self, title: str, rows: list):
        """Print a summary table. rows = [(label, value), ...]"""
        if self.is_minimal:
            self._print_plain(f"\n{title}")
            for label, value in rows:
                self._print_plain(f"  {label}: {value}")
            return

        if self._rich_available and self._console:
            from rich.table import Table
            table = Table(title=title, show_header=False, border_style="dim")
            table.add_column("Label", style="bold")
            table.add_column("Value")
            for label, value in rows:
                table.add_row(label, str(value))
            self._console.print(table)
        else:
            self._print_plain(f"\n{title}")
            for label, value in rows:
                self._print_plain(f"  {label}: {value}")

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self):
        """Close log file handle if open."""
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None


# ── Global Instance ───────────────────────────────────────────────────────────
log = Logger()
