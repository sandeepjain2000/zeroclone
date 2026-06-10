"""Shared file + console logging for the email pipeline scripts."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"

# Unicode punctuation that breaks on Windows cp1252 consoles / Notepad without BOM.
_ASCII_LOG_REPLACEMENTS = (
    ("\u2014", "-"),   # em dash
    ("\u2013", "-"),   # en dash
    ("\u2192", "->"),  # right arrow
    ("\u2026", "..."), # ellipsis
    ("\u00a0", " "),   # nbsp
)


def ascii_log_text(text: str) -> str:
    """Make log lines readable in cp1252 terminals and legacy Windows viewers."""
    if not text:
        return text
    for src, dst in _ASCII_LOG_REPLACEMENTS:
        text = text.replace(src, dst)
    return text


class AsciiSafeFormatter(logging.Formatter):
    """Normalize punctuation; keep UTF-8 letters (umlauts) when viewer supports UTF-8."""

    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, str):
            record.msg = ascii_log_text(record.msg)
        if record.args:
            record.args = tuple(
                ascii_log_text(a) if isinstance(a, str) else a for a in record.args
            )
        return super().format(record)


def _ensure_utf8_stdout() -> None:
    """Best-effort UTF-8 console on Windows (Python 3.7+)."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def _configure_logger(logger: logging.Logger, handlers: list[logging.Handler]) -> None:
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    for handler in handlers:
        logger.addHandler(handler)
    logger.propagate = False


def setup_pipeline_logging(
    script_name: str,
    *,
    also_configure: tuple[str, ...] = (),
) -> Path:
    """
    Configure logging for *script_name* (e.g. extract_cycle).

    Writes ``logs/{script_name}_YYYY-MM-DD_HH-MM-SS.log`` (UTF-8 with BOM for
    Notepad) and mirrors INFO+ to stdout.

    *also_configure*: additional logger names that share the same handlers
    (e.g. validate_cycle + validate_emails in one log file).
    """
    _ensure_utf8_stdout()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = script_name.replace(" ", "_")
    log_path = LOG_DIR / f"{safe_name}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    fmt = AsciiSafeFormatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # utf-8-sig adds BOM so Windows Notepad detects UTF-8 (fixes umlauts in company names).
    fh = logging.FileHandler(log_path, encoding="utf-8-sig", mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    handlers = [fh, sh]

    primary = get_logger(script_name)
    _configure_logger(primary, handlers)
    primary.info("Log file: %s", log_path)

    for name in also_configure:
        _configure_logger(get_logger(name), list(handlers))

    return log_path


def get_logger(script_name: str) -> logging.Logger:
    return logging.getLogger(script_name.replace(" ", "_"))
