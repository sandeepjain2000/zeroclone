"""Shared file + console logging for the email pipeline scripts."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"


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

    Writes ``logs/{script_name}_YYYY-MM-DD_HH-MM-SS.log`` and mirrors INFO+ to stdout.

    *also_configure*: additional logger names that share the same handlers
    (e.g. validate_cycle + validate_emails in one log file).
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = script_name.replace(" ", "_")
    log_path = LOG_DIR / f"{safe_name}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_path, encoding="utf-8", mode="w")
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
