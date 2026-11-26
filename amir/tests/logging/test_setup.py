import json
import logging
from datetime import UTC, datetime

import pytest

from amir.logging.setup import JsonFormatter, RedactingFilter, configure_logging

SECRET = "s3cr3t-value-XYZ"


def _record(created: datetime | None = None, msg="hello", args=None, **kw) -> logging.LogRecord:
    rec = logging.LogRecord("amir.test", logging.INFO, __file__, 1, msg, args, None, **kw)
    if created is not None:
        rec.created = created.timestamp()
    return rec


@pytest.mark.parametrize(
    ("instant", "suffix"),
    [
        (datetime(2026, 10, 25, 1, 0, tzinfo=UTC), "-04:00"),
        (datetime(2026, 11, 2, 1, 0, tzinfo=UTC), "-05:00"),
    ],
)
def test_timestamp_is_new_york(instant, suffix):
    out = json.loads(JsonFormatter().format(_record(instant)))
    assert out["ts"].endswith(suffix)


class _Out:
    def __init__(self, capsys):
        self._capsys = capsys

    def getvalue(self) -> str:
        return self._capsys.readouterr().out


@pytest.fixture
def stream(capsys):
    root = logging.getLogger()
    library = logging.getLogger("discord")
    saved = (root.handlers[:], root.level, library.level)
    try:
        yield _Out(capsys)
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        library.setLevel(saved[2])


def test_secret_redacted_in_message_and_traceback(stream):
    configure_logging("INFO", [SECRET])
    log = logging.getLogger("amir.test")
    log.info("token %s", SECRET)
    try:
        raise RuntimeError(f"failed with {SECRET}")
    except RuntimeError:
        log.exception("boom")
    text = stream.getvalue()
    assert SECRET not in text
    assert "[REDACTED]" in text
    lines = [json.loads(line) for line in text.splitlines()]
    assert lines[0]["event"] == "token [REDACTED]"
    assert "[REDACTED]" in lines[1]["exc"]


def test_token_pattern_redacted():
    tok = "A" * 24 + "." + "b" * 6 + "." + "c" * 27
    rec = _record(msg="x %s", args=(tok,))
    RedactingFilter([]).filter(rec)
    assert tok not in JsonFormatter().format(rec)


def test_unicode_preserved(stream):
    configure_logging("INFO", [])
    logging.getLogger("amir.test").info("Ñandú \U0001f409 Тэнно")
    text = stream.getvalue()
    assert "Ñandú \U0001f409 Тэнно" in text
    assert json.loads(text)["event"] == "Ñandú \U0001f409 Тэнно"


def test_extra_fields_included_and_redacted(stream):
    configure_logging("INFO", [SECRET])
    logging.getLogger("amir.test").info("hi", extra={"cycle": "cetus", "k": SECRET})
    out = json.loads(stream.getvalue())
    assert out["cycle"] == "cetus"
    assert out["k"] == "[REDACTED]"
    assert set(out) >= {"ts", "level", "logger", "event"}


def test_configure_is_idempotent(stream):
    configure_logging("INFO", [])
    configure_logging("DEBUG", [])
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_amir_handler", False)]
    assert len(ours) == 1
    assert logging.getLogger().level == logging.DEBUG
    logging.getLogger("amir.test").debug("once")
    assert len(stream.getvalue().splitlines()) == 1


def test_nested_extra_redacted(stream):
    configure_logging("INFO", [SECRET])
    logging.getLogger("amir.test").info("hi", extra={"ctx": {"auth": SECRET, "l": [SECRET]}})
    text = stream.getvalue()
    assert SECRET not in text
    assert json.loads(text)["ctx"]["auth"] == "[REDACTED]"


def test_object_extra_with_secret_in_str_redacted(stream):
    class Thing:
        def __str__(self) -> str:
            return f"Thing({SECRET})"

    configure_logging("INFO", [SECRET])
    logging.getLogger("amir.test").info("hi", extra={"obj": Thing(), "err": ValueError(SECRET)})
    text = stream.getvalue()
    assert SECRET not in text
    assert "[REDACTED]" in text


# discord.py's gateway logs every event payload at DEBUG; VOICE_STATE_UPDATE and GUILD_CREATE
# payloads carry member ids, usernames and nicknames. (A faked Discord payload.)
GATEWAY_PAYLOAD = {
    "t": "VOICE_STATE_UPDATE",
    "d": {"user_id": "4001", "member": {"user": {"id": "4001", "username": "someone"}}},
}


def test_debug_level_never_logs_discord_payloads(stream):
    configure_logging("DEBUG", [])
    logging.getLogger("discord.gateway").debug("WebSocket Event: %s", GATEWAY_PAYLOAD)
    logging.getLogger("discord.client").info("logging in using static token")
    logging.getLogger("amir.test").debug("Amir's own debug line")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert [line["logger"] for line in lines] == ["discord.client", "amir.test"]
    assert not logging.getLogger("discord.gateway").isEnabledFor(logging.DEBUG)


def test_discord_follows_a_stricter_configured_level(stream):
    configure_logging("WARNING", [])
    logging.getLogger("discord.client").info("logging in using static token")
    logging.getLogger("discord.client").warning("shard closed")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert [line["event"] for line in lines] == ["shard closed"]


def test_voice_debug_logs_only_discordpys_voice_connection_steps(stream):
    # Diagnosing voice connects (Discord's 4006 on a first handshake): discord.voice_state logs
    # connection states, transitions and endpoints only. The gateway stays at INFO.
    configure_logging("INFO", [], voice_debug=True)
    logging.getLogger("discord.voice_state").debug("Connection state changed to %s", "connected")
    logging.getLogger("discord.gateway").debug("WebSocket Event: %s", GATEWAY_PAYLOAD)

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert [line["logger"] for line in lines] == ["discord.voice_state"]
    assert lines[0]["event"] == "Connection state changed to connected"
    assert not logging.getLogger("discord.gateway").isEnabledFor(logging.DEBUG)


def test_voice_debug_is_off_unless_asked_for(stream):
    configure_logging("DEBUG", [], voice_debug=True)
    configure_logging("DEBUG", [])
    assert not logging.getLogger("discord.voice_state").isEnabledFor(logging.DEBUG)
