"""Tiny structured-log helper. Appends key=value context to log messages.

Usage:
    from spinlab import log
    import logging
    logger = logging.getLogger(__name__)

    log.warn(logger, "save_state timed out", path=p, slot=s, attempts=3)
    log.error(logger, "movie playback failed", exc=exc, replay_path=path)
    log.info(logger, "poller read recovered")
"""
from __future__ import annotations

import logging


def _emit(level_fn, msg: str, exc: BaseException | None, fields: dict) -> None:
    if fields:
        msg = f"{msg} " + " ".join(f"{k}={v!r}" for k, v in fields.items())
    if exc is not None:
        level_fn(msg, exc_info=exc)
    else:
        level_fn(msg)


def info(logger: logging.Logger, msg: str, *, exc: BaseException | None = None, **fields: object) -> None:
    _emit(logger.info, msg, exc, fields)


def warn(logger: logging.Logger, msg: str, *, exc: BaseException | None = None, **fields: object) -> None:
    _emit(logger.warning, msg, exc, fields)


def error(logger: logging.Logger, msg: str, *, exc: BaseException | None = None, **fields: object) -> None:
    _emit(logger.error, msg, exc, fields)
