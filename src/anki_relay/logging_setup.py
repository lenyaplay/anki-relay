"""Logging: readable text on stdout, JSON Lines in LOG_DIR with daily rotation.

Logs are only reachable on the server itself (files in ``LOG_DIR``, ``docker compose
logs``, the ``debug-bundle`` command); nothing serves them over HTTP or MCP.

Never logged at any level: note field contents, passwords, tokens (OAuth, codes,
AnkiWeb hkey) and email addresses. Tool arguments are logged through
``redact_args`` (structure and names, not contents), and a last-line redaction
pass scrubs anything that still looks like a secret.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Settings

LOG_FILE = "anki-relay.log"

# Set by the tool wrapper for the duration of one tool call; copied into worker
# threads by asyncio.to_thread, so sync events are tied to the call that caused them.
call_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "anki_relay_call", default=None
)

QUIET_LOGGERS = {
    # These log request/response bodies (card content, tokens) at DEBUG.
    "mcp": logging.INFO,
    "sse_starlette": logging.INFO,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "multipart": logging.WARNING,
    "python_multipart": logging.WARNING,
}

_SCRUB_NAMES = (
    r"access_token|refresh_token|code_verifier|client_secret|password|passwd|hkey|"
    r"authorization_code|token|code|req|state"
)
REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[redacted]"),
    (
        re.compile(rf'(?i)"({_SCRUB_NAMES})"\s*:\s*"[^"]*"'),
        r'"\1":"[redacted]"',
    ),
    (re.compile(rf"(?i)\b({_SCRUB_NAMES})=([^&\s\"',;]+)"), r"\1=[redacted]"),
]

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI-coloured duplicate of msg
}

# ---------------------------------------------------------------- redaction


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


TEXT_KEYS = {"data_base64", "front", "back", "browser_front", "browser_back", "css"}
MAX_LIST = 20
MAX_STR = 200


def _chars(value: Any) -> str:
    return f"<{len(str(value))} chars>"


def _short_url(value: Any) -> str:
    parts = urlsplit(str(value))
    return f"{parts.scheme}://{parts.hostname or ''}{parts.path}"


def redact_args(value: Any, key: str | None = None) -> Any:
    """Tool arguments for the log: names and structure kept, contents dropped.

    Kept: search queries, deck / note type / field / template / tag names, file
    names, flags, numbers. Replaced by their length: field values, base64 data,
    template text and CSS. URLs lose their query string. Long lists are cut.
    """
    if key == "fields" and isinstance(value, dict):
        return {str(k): _chars(v) for k, v in value.items()}
    if key in TEXT_KEYS and value is not None:
        return _chars(value)
    if key == "url" and value is not None:
        return _short_url(value)
    if key and key.endswith("_ids") and isinstance(value, list):
        return {"count": len(value), "first": value[:5]}
    if isinstance(value, dict):
        return {str(k): redact_args(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        items = [redact_args(v) for v in list(value)[:MAX_LIST]]
        if len(value) > MAX_LIST:
            items.append(f"<{len(value) - MAX_LIST} more>")
        return items
    if isinstance(value, str) and len(value) > MAX_STR:
        return value[:MAX_STR] + f"…<{len(value)} chars>"
    return value


def summarize_result(text: str) -> dict[str, Any]:
    """Numbers and flags from a tool's JSON answer (never names or contents)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, bool | int | float) and not k.endswith("id"):
            out[k] = v
    if isinstance(data.get("results"), list):
        out["item_errors"] = sum(1 for r in data["results"] if isinstance(r, dict) and "error" in r)
    if data.get("warnings"):
        out["warnings"] = len(data["warnings"])
    return out


# ---------------------------------------------------------------- filters & formatters


class ContextFilter(logging.Filter):
    """Adds user and call_id of the current tool call to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = call_context.get()
        if ctx:
            for k, v in ctx.items():
                if v is not None and not hasattr(record, k):
                    setattr(record, k, v)
        return True


class AccessLogFilter(logging.Filter):
    """Uvicorn access lines: drop query strings (sign-in links, OAuth state) and the
    successful /health probes of the Docker healthcheck (one every 30 s)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            args = list(record.args)
            if len(args) >= 5 and args[2] == "/health" and args[4] == 200:
                return False
            if len(args) >= 3 and isinstance(args[2], str):
                args[2] = args[2].split("?", 1)[0]
                record.args = tuple(args)
        return True


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        tags = [f"{k}={getattr(record, k)}" for k in ("user", "call_id") if hasattr(record, k)]
        if tags:
            line += " [" + " ".join(tags) + "]"
        return redact(line)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(data, ensure_ascii=False, default=str))


class PrivateTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Daily rotation; every log file is created with mode 600."""

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        with contextlib.suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return stream


# ---------------------------------------------------------------- setup

_OURS = "_anki_relay_handler"


def setup_logging(settings: Settings) -> list[logging.Handler]:
    """Configure root logging once; returns the handlers it added."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _OURS, False)]:
        root.removeHandler(handler)
        handler.close()
    added: list[logging.Handler] = []

    stdout = logging.StreamHandler(sys.stderr)
    stdout.setFormatter(TextFormatter())
    added.append(stdout)

    if settings.log_dir is not None:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(settings.log_dir, 0o700)
        file_handler = PrivateTimedRotatingFileHandler(
            Path(settings.log_dir) / LOG_FILE,
            when="midnight",
            backupCount=settings.log_retention_days,
            utc=True,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        added.append(file_handler)

    for handler in added:
        setattr(handler, _OURS, True)
        handler.addFilter(ContextFilter())
        handler.addFilter(AccessLogFilter())
        root.addHandler(handler)

    root.setLevel(settings.log_level)
    numeric = logging.getLevelName(settings.log_level)
    for name, floor in QUIET_LOGGERS.items():
        logging.getLogger(name).setLevel(max(floor, numeric))
    # Let uvicorn's loggers reach our handlers (uvicorn.run(log_config=None)).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    return added


def remove_handlers(handlers: list[logging.Handler]) -> None:
    root = logging.getLogger()
    for handler in handlers:
        root.removeHandler(handler)
        handler.close()
