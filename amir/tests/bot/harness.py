"""Harness for the /amir report and command tests (the fixtures are in conftest.py).

The services are the real ones from build_services, over a real database, fed by the real
2026-10-24 captures. Only the Discord edge is faked: the sender (recording; while publishing is
off the gate never even holds it), the voice connector and the player. The settings carry a
canary token and container-style paths (/data, /app) that no reply may ever show.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from amir.bot.client import AmirBot, Services
from amir.config import load_settings
from amir.config.settings import PublishingSettings, Settings
from amir.net.http import FetchResult
from amir.publishing.cards import Card
from amir.publishing.gate import ChannelKey, SendResult
from amir.storage.db import Database
from amir.voice.follow import ChannelView, VoiceCensus

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "amir.example.toml"
TOKEN = "NzAxMDAx.canary-token.never-shown"
GUILD_ID = 2001
VOICE_ID = 3001
NEWS_ID = 3002
CYCLES_ID = 3003
VOICE_LOG_ID = 3004
PREDECESSOR_ID = 3005  # another voice channel Amir may follow people into
AFK_ID = 3006  # the server's AFK channel: logged, never one he sits in
MEMBER_ID = 4001
OTHER_MEMBER_ID = 4002
THIRD_MEMBER_ID = 4003

FORUM_CAPTURE = "forum_pc_update_notes.rss.xml"
STEAM_CAPTURE = "steam_getnewsforapp_announcements.json"
OFFICIAL_CAPTURE = "official_worldstate_baro_present.json"
WFCD_CAPTURE = "wfcd_pc_baro_present.json"
ITEMS_CAPTURE = "wfcd_items_names.json"
NEWS_CAPTURES = {"forum_rss": FORUM_CAPTURE, "steam_news": STEAM_CAPTURE}


def make_settings(root: Path, ffmpeg: str, *, publishing: bool = False) -> Settings:
    """The example config with Discord IDs set, the token from DISCORD_TOKEN, the clips under an
    `app` directory and the database and heartbeat under /data, as in the container."""
    base = load_settings(EXAMPLE, {"DISCORD_TOKEN": TOKEN})
    ids = {
        "guild_id": GUILD_ID,
        "voice_channel_id": VOICE_ID,
        "news_channel_id": NEWS_ID,
        "cycles_channel_id": CYCLES_ID,
        "voice_log_channel_id": VOICE_LOG_ID,
    }
    greetings = root / "app" / "assets" / "greetings"
    return base.model_copy(
        update={
            "discord": base.discord.model_copy(update=ids),
            "publishing": PublishingSettings(enabled=publishing),
            "voice": base.voice.model_copy(
                update={"greetings_dir": str(greetings), "ffmpeg_path": ffmpeg}
            ),
            # Never opened by the harness (the database is opened under tmp_path): these are
            # canaries for the leak checks.
            "storage": base.storage.model_copy(update={"db_path": "/data/amir.sqlite3"}),
            "logging": base.logging.model_copy(update={"heartbeat_path": "/data/heartbeat"}),
        }
    )


def without_item(rss: bytes, guid: str) -> bytes:
    """A copy of the real forum feed with the <item> whose guid is `guid` removed."""
    needle = f">{guid}</guid>".encode()
    out, removed = re.subn(
        rb"<item>(?:(?!</item>).)*?" + re.escape(needle) + rb".*?</item>", b"", rss, flags=re.S
    )
    assert removed == 1
    return out


# --- fakes at the Discord edge ----------------------------------------------------------------


class RecordingSender:
    """Replaces DiscordSender: records every call. With publishing off the gate drops it at
    construction, so its call list must stay empty."""

    def __init__(self, client: Any, channel_ids: dict[ChannelKey, int]) -> None:
        self.channel_ids = dict(channel_ids)
        self.calls: list[tuple[Any, ...]] = []
        self._next_id = 9001

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        self.calls.append(("send", channel, list(cards), text, role_ping_id))
        self._next_id += 1
        return SendResult("sent", self._next_id)

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        self.calls.append(("edit", channel, message_id, list(cards)))
        return SendResult("sent", message_id)

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        self.calls.append(("find_recent", channel))
        return None


class FakeConnector:
    """Replaces DiscordVoiceConnector. While `connected`, Amir is in `channel_id`: Warframe
    unless a connect said otherwise. `targets` records the channel of each connect."""

    def __init__(self, client: Any) -> None:
        self.channel_id = VOICE_ID
        self.connected = False
        self.connects = 0
        self.targets: list[int] = []
        self.disconnects = 0
        self.connect_error: Exception | None = None  # raised by every connect while set

    async def connect(
        self,
        channel_id: int,
        timeout: float,  # noqa: ASYNC109 - protocol signature
    ) -> None:
        self.connects += 1
        self.targets.append(channel_id)
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        self.channel_id = channel_id

    async def disconnect(self) -> bool:
        self.disconnects += 1
        was, self.connected = self.connected, False
        return was

    def is_connected(self) -> bool:
        return self.connected

    def current_channel_id(self) -> int | None:
        return self.channel_id if self.connected else None

    def voice_client(self) -> None:
        return None


class FakeKickDetector:
    """Replaces DiscordKickDetector: never reads an audit log; `kicked` is its answer."""

    def __init__(self, client: Any, guild_id: int, clock: Any) -> None:
        self.client = client
        self.guild_id = guild_id
        self.clock = clock
        self.kicked = False
        self.snapshots = 0
        self.asked = 0
        self.gate: asyncio.Event | None = None  # set: every answer waits until it opens
        self.asking = asyncio.Event()  # set once a kick check waits at the gate

    async def snapshot(self, keep: Any = None) -> None:
        self.snapshots += 1

    async def was_kicked(self) -> bool:
        self.asked += 1
        if self.gate is not None:
            self.asking.set()
            await self.gate.wait()
        return self.kicked


class FakePlayer:
    """Replaces DiscordPlayer: records each clip played."""

    def __init__(self, voice_client_provider: Any, ffmpeg: str, audio_filter: str) -> None:
        self.played: list[Path] = []
        self.finished = asyncio.Event()  # set once a clip has played

    async def play(self, path: Path, listener_id: int | None) -> None:
        self.played.append(path)
        self.finished.set()


class CaptureFetcher:
    """No network: every configured URL answers with its real capture. `queued[url]` holds
    bodies served first, in order, before the capture."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = dict(bodies)
        self.queued: dict[str, list[bytes]] = {}
        self.calls: list[str] = []

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        self.calls.append(url)
        queued = self.queued.get(url)
        body = queued.pop(0) if queued else self.bodies[url]
        return FetchResult(200, body, None, None, False)


class FakeHttpSession:
    async def close(self) -> None:
        pass


class Member:
    """A member as the voice census counts them."""

    def __init__(self, member_id: int, *, bot: bool = False) -> None:
        self.id = member_id
        self.bot = bot


def census_of(members: list[Member], home: int) -> VoiceCensus:
    """The census with `members` (bots left out, as the real reader does) in the home channel."""
    humans = frozenset(m.id for m in members if not m.bot)
    return VoiceCensus(
        channels=(ChannelView(home, humans, candidate=True, can_speak=True),), afk_channel_id=None
    )


def server_census(channels: dict[int, list[Member]], silent: Collection[int] = ()) -> VoiceCensus:
    """The census of every voice channel in `channels`, in that (server) order: bots left out, as
    the real reader does, and AFK_ID the AFK channel, never a candidate. Amir's role may not
    speak in the `silent` channels, so they are no candidates either."""
    views = tuple(
        ChannelView(
            channel_id,
            frozenset(m.id for m in members if not m.bot),
            candidate=channel_id != AFK_ID and channel_id not in silent,
            can_speak=channel_id not in silent,
        )
        for channel_id, members in channels.items()
    )
    return VoiceCensus(channels=views, afk_channel_id=AFK_ID)


def record_writer(monkeypatch: pytest.MonkeyPatch, writer: Any) -> list[tuple[Any, ...]]:
    """Every call that reaches the GatedWriter, recorded and passed through."""
    calls: list[tuple[Any, ...]] = []
    for name in ("send", "edit", "find_recent"):
        real = getattr(writer, name)

        def recording(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            calls.append((_name, *args, kwargs))
            return _real(*args, **kwargs)

        monkeypatch.setattr(writer, name, recording)
    return calls


# --- harness ----------------------------------------------------------------------------------


@dataclass
class App:
    settings: Settings
    db: Database
    clock: Any
    fetcher: CaptureFetcher
    bot: AmirBot
    senders: list[RecordingSender]
    connectors: list[FakeConnector]
    players: list[FakePlayer]
    kick_detectors: list[FakeKickDetector]
    members: list[Member]  # who is in Warframe
    elsewhere: dict[int, list[Member]]  # who is in Arcade and in AFK, after it in order
    writer_calls: list[tuple[Any, ...]]
    greetings_dir: Path
    silent: set[int]  # the voice channels where Amir's role may not speak

    @property
    def services(self) -> Services:
        return self.bot.services

    @property
    def sender(self) -> RecordingSender:
        [sender] = self.senders
        return sender

    @property
    def connector(self) -> FakeConnector:
        [connector] = self.connectors
        return connector

    @property
    def player(self) -> FakePlayer:
        [player] = self.players
        return player

    @property
    def kicks(self) -> FakeKickDetector:
        [detector] = self.kick_detectors
        return detector

    async def poll_world(self) -> None:
        """Read the world state once: cycles, Baro and the item names, from the captures."""
        await self.services.worldstate.poll_once()


MakeApp = Callable[..., Awaitable[App]]
