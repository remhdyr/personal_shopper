"""Tests for logging configuration, especially local log persistence."""

from __future__ import annotations

import logging

from shopper.logging_setup import get_logger, setup_logging


def test_logs_are_written_to_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPER_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("SHOPPER_LOG_LEVEL", "INFO")
    try:
        setup_logging()
        get_logger("shopper.test").info("hello from the last run")
        logging.shutdown()

        log_file = tmp_path / "shopper.log"
        assert log_file.exists()
        assert "hello from the last run" in log_file.read_text(encoding="utf-8")
    finally:
        # Drop the file handlers so the tmp_path can be cleaned up and later
        # tests aren't left writing into a deleted directory.
        for handler in list(logging.getLogger().handlers):
            handler.close()
            logging.getLogger().removeHandler(handler)


def test_file_logging_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPER_LOG_DIR", "")
    try:
        setup_logging()
        assert not (tmp_path / "shopper.log").exists()
    finally:
        for handler in list(logging.getLogger().handlers):
            handler.close()
            logging.getLogger().removeHandler(handler)
