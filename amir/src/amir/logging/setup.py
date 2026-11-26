"""JSON-lines logging to stdout: America/New_York timestamps, secrets redacted."""

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")
_REDACTED = "[REDACTED]"
_TOKEN_RE = re.compile(r"[\w-]{24,}\.[\w-]{6}\.[\w-]{27,}")
_STANDARD_ATTRS = frozenset(
    set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
)
_HANDLER_MARK = "_amir_handler"
_DISCORD_LOGGER = "discord"
_DISCORD_VOICE_STATE_LOGGER = "discord.voice_state"


class RedactingFilter(logging.Filter):
    """Replaces secrets and token-shaped strings in messages, args, extras and exceptions."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        return _TOKEN_RE.sub(_REDACTED, text)

    def _clean(self, value: Any) -> Any:
        return self.redact(value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        for key in set(vars(record)) - _STANDARD_ATTRS:
            setattr(record, key, self._clean(getattr(record, key)))
        return True


class JsonFormatter(logging.Formatter):
    """Serialises records as JSON lines; the final line is redacted so nothing can bypass it."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._redactor = RedactingFilter(secrets)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, _NY).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        line = json.dumps(payload, ensure_ascii=False, default=str)
        return self._redactor.redact(line)


def configure_logging(level: str, secrets: Iterable[str], *, voice_debug: bool = False) -> None:
    """Install a single JSON stdout handler on the root logger (safe to call repeatedly).

    discord.py's loggers never go below INFO, whatever `level` is: at DEBUG its gateway logs
    every event payload, and voice-state and guild payloads carry member ids and names.
    `voice_debug` is the one exception: discord.voice_state at DEBUG, which logs only voice
    connection states, transitions and endpoints, to diagnose voice connects.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter(secrets))
    handler.setFormatter(JsonFormatter(secrets))
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger(_DISCORD_LOGGER).setLevel(max(root.level, logging.INFO))
    voice_state = logging.getLogger(_DISCORD_VOICE_STATE_LOGGER)
    voice_state.setLevel(logging.DEBUG if voice_debug else logging.NOTSET)
