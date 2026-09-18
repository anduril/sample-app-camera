"""Structured JSON logging: one JSON object per line, journald-friendly.

Emits one JSON object per line to stdout with a stable set of keys plus any
structured fields passed via ``extra={...}``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# Keys that live on every LogRecord; anything else is treated as a structured
# field and included in the JSON output.
_RESERVED = set(logging.makeLogRecord({}).__dict__.keys()) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class StructuredLogger:
    """Thin wrapper letting call sites pass structured fields as kwargs.

    ``logger.info("msg", entity_id=..., count=3)`` routes the fields into the
    LogRecord's ``extra`` so the JSON formatter can emit them as key/value
    fields.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _log(self, level: int, msg: str, fields: dict) -> None:
        exc_info = fields.pop("exc_info", None)
        self._logger.log(level, msg, extra=fields, exc_info=exc_info)

    def debug(self, msg: str, **fields) -> None:
        self._log(logging.DEBUG, msg, fields)

    def info(self, msg: str, **fields) -> None:
        self._log(logging.INFO, msg, fields)

    def warning(self, msg: str, **fields) -> None:
        self._log(logging.WARNING, msg, fields)

    def error(self, msg: str, **fields) -> None:
        self._log(logging.ERROR, msg, fields)


def configure(debug: bool = False) -> None:
    """Install the JSON formatter on the root logger."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)


def get_logger(name: str = "lattice-cam") -> StructuredLogger:
    return StructuredLogger(logging.getLogger(name))
