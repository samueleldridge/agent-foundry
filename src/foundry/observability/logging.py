"""Structured logging threaded with run_id.

Phase 1 configures structlog for console output and provides
``run_logger(run_id)`` — a BoundLogger with ``run_id`` stamped on every line.
OTel correlation and file sinks land in Phase 9.
"""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False


def _stderr_logger_factory(*args: object) -> structlog.PrintLogger:
    # Resolve sys.stderr at logger-creation time (not configure time) so
    # test harnesses that swap stderr (pytest capsys) never leave a cached
    # logger writing to a closed stream.
    return structlog.PrintLogger(sys.stderr)


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=False,
    )
    _CONFIGURED = True


def run_logger(run_id: str) -> structlog.typing.FilteringBoundLogger:
    """A logger with run_id bound onto every emitted line."""
    configure_logging()
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger()
    return logger.bind(run_id=run_id)


__all__ = ["configure_logging", "run_logger"]
