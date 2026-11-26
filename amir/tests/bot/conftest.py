import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import amir.bot.client as client_module
import amir.voice.companion as companion_module
from amir.bot.client import AmirBot, build_services
from amir.config.settings import Settings
from amir.storage.db import Database

from .harness import (
    AFK_ID,
    ITEMS_CAPTURE,
    NEWS_CAPTURES,
    OFFICIAL_CAPTURE,
    PREDECESSOR_ID,
    WFCD_CAPTURE,
    App,
    CaptureFetcher,
    FakeConnector,
    FakeHttpSession,
    FakeKickDetector,
    FakePlayer,
    MakeApp,
    Member,
    RecordingSender,
    make_settings,
    record_writer,
    server_census,
)


def install_clip(greetings: Path, clip: Path) -> None:
    """The greetings directory, holding a copy of the test clip."""
    greetings.mkdir(parents=True)
    shutil.copy(clip, greetings / clip.name)


@pytest.fixture
async def make_app(
    tmp_path: Path, ffmpeg_exe: str, voice_clip: Path, captured, clock, monkeypatch
) -> AsyncIterator[MakeApp]:
    """Builds an App: the real services wired by build_services, on the shared test clock."""
    apps: list[App] = []
    current: dict[str, list[Any]] = {}

    def sender(*args: Any) -> RecordingSender:
        current["senders"].append(RecordingSender(*args))
        return current["senders"][-1]

    def connector(*args: Any) -> FakeConnector:
        current["connectors"].append(FakeConnector(*args))
        return current["connectors"][-1]

    def player(*args: Any) -> FakePlayer:
        current["players"].append(FakePlayer(*args))
        return current["players"][-1]

    def kick_detector(*args: Any) -> FakeKickDetector:
        current["kick_detectors"].append(FakeKickDetector(*args))
        return current["kick_detectors"][-1]

    monkeypatch.setattr(client_module, "SystemClock", lambda: clock)
    monkeypatch.setattr(client_module, "DiscordSender", sender)
    monkeypatch.setattr(client_module, "DiscordVoiceConnector", connector)
    monkeypatch.setattr(client_module, "DiscordPlayer", player)
    monkeypatch.setattr(client_module, "DiscordKickDetector", kick_detector)
    monkeypatch.setattr(companion_module, "KICK_CHECK_DELAY_SECONDS", 0)  # no 2 s waits
    monkeypatch.setattr(companion_module, "AUDIT_REFRESH_DELAY_SECONDS", 0)

    async def make(*, publishing: bool = False, settings: Settings | None = None) -> App:
        root = tmp_path / f"app{len(apps)}"
        settings = settings or make_settings(root, ffmpeg_exe, publishing=publishing)
        greetings = Path(settings.voice.greetings_dir)
        await asyncio.to_thread(install_clip, greetings, voice_clip)
        db = await Database.open(root / "amir.sqlite3")
        await db.migrate()
        ws = settings.worldstate
        bodies = {
            ws.official_url: captured(OFFICIAL_CAPTURE),
            ws.wfcd_url: captured(WFCD_CAPTURE),
            ws.items_url: captured(ITEMS_CAPTURE),
        }
        bodies.update({s.url: captured(NEWS_CAPTURES[s.kind]) for s in settings.news.sources})
        fetcher = CaptureFetcher(bodies)
        members: list[Member] = []
        elsewhere: dict[int, list[Member]] = {PREDECESSOR_ID: [], AFK_ID: []}
        silent: set[int] = set()
        current["senders"], current["connectors"], current["players"] = [], [], []
        current["kick_detectors"] = []
        bot = AmirBot(
            settings,
            lambda client: build_services(client, settings, db, FakeHttpSession(), fetcher),
            voice_census=lambda: server_census(
                {settings.discord.voice_channel_id: members, **elsewhere}, silent
            ),
        )
        app = App(
            settings=settings,
            db=db,
            clock=clock,
            fetcher=fetcher,
            bot=bot,
            senders=current["senders"],
            connectors=current["connectors"],
            players=current["players"],
            kick_detectors=current["kick_detectors"],
            members=members,
            elsewhere=elsewhere,
            writer_calls=record_writer(monkeypatch, bot.services.writer),
            greetings_dir=greetings,
            silent=silent,
        )
        apps.append(app)
        return app

    yield make
    for app in apps:
        s = app.services
        await s.supervisor.cancel_all()
        await s.companion.cancel_background()
        s.session.cancel_disconnect()
        await app.db.close()


@pytest.fixture
async def app(make_app: MakeApp) -> App:
    """Publishing off, the default."""
    return await make_app()
