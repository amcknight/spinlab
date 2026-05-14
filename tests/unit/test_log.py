"""Tests for spinlab.log — tiny structured-log helper."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from spinlab import log


def test_warn_formats_fields_as_key_value(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.warn")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log.warn(logger, "save_state timed out", path="/foo/bar.state", slot=9999)
    assert len(caplog.records) == 1
    rec = caplog.records[0]
    assert rec.levelno == logging.WARNING
    # repr-based: strings get quoted, ints stay bare.
    assert rec.getMessage() == "save_state timed out path='/foo/bar.state' slot=9999"


def test_info_formats_fields(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.info")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log.info(logger, "poller read recovered")
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert caplog.records[0].getMessage() == "poller read recovered"


def test_warn_with_no_fields_emits_clean_message(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.nofields")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log.warn(logger, "bare message")
    assert caplog.records[0].getMessage() == "bare message"  # no trailing space


def test_error_with_exc_attaches_traceback(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.exc")
    try:
        raise ValueError("boom")
    except ValueError as exc:
        with caplog.at_level(logging.ERROR, logger=logger.name):
            log.error(logger, "movie playback failed", exc=exc, replay_path="/r.replay")
    rec = caplog.records[0]
    assert rec.levelno == logging.ERROR
    assert rec.getMessage() == "movie playback failed replay_path='/r.replay'"
    assert rec.exc_info is not None
    assert rec.exc_info[0] is ValueError


def test_path_repr_renders_correctly(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.path")
    p = Path("/tmp/foo")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log.warn(logger, "msg", path=p)
    msg = caplog.records[0].getMessage()
    # Path repr varies by OS (PosixPath vs WindowsPath); just verify the path is present.
    assert "path=" in msg
    assert "foo" in msg
