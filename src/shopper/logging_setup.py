"""Central logging setup."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging() -> None:
    level = os.environ.get("SHOPPER_LOG_LEVEL", "INFO").upper()
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    # Persist logs to a rotating file so past runs can be inspected later.
    # Location defaults to data/shopper.log next to the SQLite database; set
    # SHOPPER_LOG_DIR to relocate it (empty string disables file logging).
    log_dir = os.environ.get("SHOPPER_LOG_DIR", "data")
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path / "shopper.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )

    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    # Quiet down noisy third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
