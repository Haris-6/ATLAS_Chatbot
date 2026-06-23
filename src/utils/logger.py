"""Centralized logging setup for ATLAS.

A single configured logger that all modules import. The format is structured
enough for both local development and later ingestion by Azure Application
Insights without changes.
"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """Return a logger configured with ATLAS's standard format.

    Safe to call multiple times for the same name - handlers are only
    attached once to avoid duplicate log lines.

    Args:
        name: Logger name. Pass `__name__` from the calling module.
        level: Logging level (DEBUG, INFO, WARNING, ERROR).

    Returns:
        A configured `logging.Logger` instance.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(handler)
    # Stop propagation so we don't double-log via the root logger
    logger.propagate = False
    return logger
