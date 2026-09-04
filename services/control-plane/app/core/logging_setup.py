"""Structured logging.

One JSON object per line on stdout. That is the contract every container log
pipeline expects: Docker/containerd captures stdout, and a shipper (Fluent Bit,
Promtail, the Azure Monitor agent, the CloudWatch agent) forwards it without the
application needing to know which one is downstream. The application never
writes log files - see docs/LOGGING.md for why.

Two extras matter:
  * `extra={"context": {...}}` merges structured fields into the record
  * a correlation id is carried per-request in a ContextVar so every line
    emitted while serving a request can be joined together later
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

# Attributes LogRecord always carries; anything else was added by the caller and
# is worth promoting into the JSON payload.
_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str:
    return _correlation_id.get()


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON document."""

    def __init__(self, service: str, environment: str, version: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service,
            "environment": self.environment,
            "version": self.version,
        }

        correlation = _correlation_id.get()
        if correlation:
            payload["correlation_id"] = correlation

        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)

        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "context" and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Human-readable fallback for `LOG_FORMAT=text` during local debugging."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name:<28} {record.getMessage()}"
        context = getattr(record, "context", None)
        if isinstance(context, dict) and context:
            base += "  " + " ".join(f"{k}={v}" for k, v in context.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    service: str = "cloudops",
    environment: str = "local",
    version: str = "0.0.0",
) -> None:
    """Install a single stdout handler on the root logger."""
    formatter: logging.Formatter = (
        TextFormatter() if fmt == "text" else JsonFormatter(service, environment, version)
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # uvicorn installs its own colourised handlers; strip them so that every
    # line in the container - ours and the server's - is one JSON schema.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # The access log is emitted by our own middleware, which knows the
    # correlation id, the route template and the duration. Letting uvicorn also
    # log every request would double the log volume and give the shipper two
    # different schemas for the same event.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    # httpx logs every outbound scrape at INFO, which drowns the real signal.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
