"""Structured logging configuration (improvements-plan WP 12.4, WS-09).

Provides :class:`JsonFormatter`, a one-line-per-record JSON formatter for
daemon/service mode, selected with ``girder daemon --log-format json``. The
default remains human-readable text.

The module is named ``logging_config`` (not ``logging``) on purpose: a
submodule shadowing the stdlib name is legal in Python 3 but invites
confusing imports.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

__all__ = ["JsonFormatter", "configure_logging"]


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object with a fixed key set.

    Keys: ``ts`` (ISO-8601 UTC), ``level``, ``logger``, ``msg``, ``module``,
    plus ``exc`` (formatted traceback) when ``record.exc_info`` is set.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(
    *,
    level: int,
    fmt: str = "text",
    **basic_config_kwargs: Any,
) -> None:
    """Configure the root logger for CLI entry points.

    ``fmt="text"`` keeps the classic human-readable format (the default);
    ``fmt="json"`` switches every record to :class:`JsonFormatter`. Extra
    keyword arguments are forwarded to ``logging.basicConfig``.
    """
    if fmt == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logging.basicConfig(level=level, handlers=[handler], **basic_config_kwargs)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            **basic_config_kwargs,
        )
