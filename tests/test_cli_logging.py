"""Tests for file-based logging setup."""
import logging
from pathlib import Path

from spinlab.cli import _setup_file_logging


def test_setup_file_logging_creates_log_file(tmp_path):
    """_setup_file_logging creates a rotating log file in data_dir."""
    _setup_file_logging(tmp_path)

    log_path = tmp_path / "spinlab.log"
    logger = logging.getLogger("spinlab.test_logging")
    logger.info("test message from logging test")

    for handler in logging.root.handlers:
        handler.flush()

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "test message from logging test" in content


def test_setup_file_logging_handler_is_rotating(tmp_path):
    """The file handler should be a RotatingFileHandler."""
    from logging.handlers import RotatingFileHandler

    _setup_file_logging(tmp_path)

    rotating_handlers = [
        h for h in logging.root.handlers
        if isinstance(h, RotatingFileHandler)
        and str(tmp_path) in str(h.baseFilename)
    ]
    assert len(rotating_handlers) == 1
    assert rotating_handlers[0].maxBytes == 1_000_000
    assert rotating_handlers[0].backupCount == 3


def teardown_function():
    """Clean up any handlers we added to root logger."""
    from logging.handlers import RotatingFileHandler
    for h in logging.root.handlers[:]:
        if isinstance(h, RotatingFileHandler):
            logging.root.removeHandler(h)
            h.close()
