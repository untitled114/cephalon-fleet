import asyncio
import contextlib
import logging
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands

from amir import __version__
from amir.bot.commands import (
    HOLD_CLEARED,
    NO_CLIPS,
    NOT_ADMIN,
    NOT_IN_VOICE,
    TEST_MEMBER_ID,
    TEST_TITLE,
    build_amir_group,
    ensure_admin,
    register_amir_commands,
)
from amir.bot.status import config_report, snapshot, sources_report
from amir.news.forum_rss import parse_forum_rss
from amir.publishing.gate import ChannelKey
from amir.storage.repos import OutboxRepo, PreviewRepo, VoiceHold, VoiceHoldRepo
from amir.voice.classify import VoiceEventKind
from amir.voice.companion import VoiceEvent
from amir.voice.session import VoiceConfigError

from .harness import (
    AFK_ID,
    FORUM_CAPTURE,
    GUILD_ID,
    MEMBER_ID,
    OTHER_MEMBER_ID,
    PREDECESSOR_ID,
    THIRD_MEMBER_ID,
    TOKEN,
    VOICE_ID,
    App,
    Member,
    without_item,
)

LEAVES = (
    "status",
    "health",
    "version",
    "voice status",
    "voice reconnect",
    "voice test",
    "voice greetings",
    "news status",
    "news check",
    "news sources",
    "news test",
    "config validate",
    "cycles refresh",
    "baro next",
)
DEFERRED = ("news check", "voice greetings", "voice reconnect", "cycles refresh")


# --- a fake Interaction -----------------------------------------------------------------------


class FakeResponse:
    """discord.InteractionResponse: records send_message and defer."""

    def __init__(self, interaction: "FakeInteraction") -> None:
        self._interaction = interaction
        self.fail_with: Exception | None = None  # raised by every send_message while set

    def is_done(self) -> bool:
        return any(kind in ("send_message", "defer") for kind, _, _ in self._interaction.calls)

    async def send_message(self, content: str | None = None, **kwargs: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        assert not self.is_done(), "an interaction is answered once"
        self._interaction.calls.append(("send_message", content, kwargs))

    async def defer(self, **kwargs: Any) -> None:
        assert not self.is_done(), "an interaction is answered once"
        self._interaction.calls.append(("defer", None, kwargs))


class FakeFollowup:
    """discord.Webhook as interaction.followup: records send."""

    def __init__(self, interaction: "FakeInteraction") -> None:
        self._interaction = interaction

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        assert self._interaction.response.is_done(), "a follow-up needs a response first"
        self._interaction.calls.append(("followup", content, kwargs))


class FakeInteraction:
    def __init__(self, *, guild_id: int | None = GUILD_ID, admin: bool = True) -> None:
        self.guild_id = guild_id
        self.user = SimpleNamespace(guild_permissions=discord.Permissions(administrator=admin))
        self.calls: list[tuple[str, str | None, dict[str, Any]]] = []
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)

    @property
    def replies(self) -> list[tuple[str | None, dict[str, Any]]]:
        """Every message sent back, whether as the response or as a follow-up."""
        return [(content, kw) for kind, content, kw in self.calls if kind != "defer"]

    @property
    def text(self) -> str:
        return "\n".join(content or "" for content, _ in self.replies)


def command(group: app_commands.Group, path: str) -> app_commands.Command:
    node: Any = group
    for name in path.split():
        node = node.get_command(name)
    assert isinstance(node, app_commands.Command)
    return node


async def run(app: App, path: str, **interaction: Any) -> FakeInteraction:
    fake = FakeInteraction(**interaction)
    await command(build_amir_group(app.services, app.settings), path).callback(fake)
    return fake


def spy(monkeypatch, obj: object, name: str) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    real = getattr(obj, name)

    async def wrapper(*args: Any) -> Any:
        calls.append(args)
        return await real(*args)

    monkeypatch.setattr(obj, name, wrapper)
    return calls


async def kick(app: App) -> None:
    """A moderator disconnects Amir from voice: he is held out."""
    app.kicks.kicked = True
    app.connector.connected = False
    await app.services.companion.on_own_disconnect()
    assert app.services.companion.hold is not None


def assert_ephemeral(fake: FakeInteraction) -> None:
    assert fake.replies
    for kind, _, kwargs in fake.calls:
        assert kwargs["ephemeral"] is True, kind
        if kind != "defer":
            assert kwargs["allowed_mentions"].to_dict() == {"parse": []}


# --- shape and the admin check ----------------------------------------------------------------


async def test_group_shape(app):
    group = build_amir_group(app.services, app.settings)

    shape = {
        child.name: (
            sorted(c.name for c in child.commands)
            if isinstance(child, app_commands.Group)
            else None
        )
        for child in group.commands
    }

    assert group.name == "amir"
    assert shape == {
        "status": None,
        "health": None,
        "version": None,
        "voice": ["greetings", "reconnect", "status", "test"],
        "news": ["check", "sources", "status", "test"],
        "config": ["validate"],
        "cycles": ["refresh"],
        "baro": ["next"],
    }
    assert group.guild_only is True
    assert group.default_permissions == discord.Permissions(administrator=True)
    assert all(command(group, path).description for path in LEAVES)


async def test_non_admin_refused():
    fake = FakeInteraction(admin=False)

    assert await ensure_admin(fake, GUILD_ID) is False

    assert fake.replies == [(NOT_ADMIN, fake.replies[0][1])]
    assert_ephemeral(fake)


@pytest.mark.parametrize("guild_id", [GUILD_ID + 1, None], ids=["other guild", "direct message"])
async def test_wrong_guild_refused(guild_id):
    fake = FakeInteraction(guild_id=guild_id)

    assert await ensure_admin(fake, GUILD_ID) is False

    assert [content for content, _ in fake.replies] == [NOT_ADMIN]
    assert_ephemeral(fake)


async def test_user_without_guild_permissions_refused():
    fake = FakeInteraction()
    fake.user = SimpleNamespace()  # a discord.User, as in a DM: no guild permissions

    assert await ensure_admin(fake, GUILD_ID) is False
    assert [content for content, _ in fake.replies] == [NOT_ADMIN]


async def test_admin_allowed():
    fake = FakeInteraction()

    assert await ensure_admin(fake, GUILD_ID) is True

    assert fake.calls == []


@pytest.mark.parametrize(
    "who",
    [{"admin": False}, {"guild_id": GUILD_ID + 1}],
    ids=["non-admin", "other guild"],
)
@pytest.mark.parametrize("path", LEAVES)
async def test_every_command_checks_admin_at_runtime(path, who, app, monkeypatch):
    polls = spy(monkeypatch, app.services.news, "poll_all")
    scans = spy(monkeypatch, app.services.library, "scan")
    ticks = spy(monkeypatch, app.services.cycles, "tick")
    app.members.append(Member(MEMBER_ID))
    # So voice test would act if it got through.
    await app.services.session.ensure_connected(VOICE_ID)
    connects = app.connector.connects

    fake = await run(app, path, **who)

    assert [content for content, _ in fake.replies] == [NOT_ADMIN]
    assert [kind for kind, _, _ in fake.calls] == ["send_message"]  # refused before any defer
    assert_ephemeral(fake)
    assert (polls, scans, ticks) == ([], [], [])
    assert app.fetcher.calls == []
    assert app.writer_calls == []
    assert (app.connector.connects, app.connector.disconnects) == (connects, 0)
    assert len(app.services.greetings) == 0


@pytest.mark.parametrize("path", LEAVES)
async def test_every_command_answers_ephemerally_and_never_posts(path, app):
    await app.poll_world()
    app.members.append(Member(MEMBER_ID))

    fake = await run(app, path)

    assert_ephemeral(fake)
    assert TOKEN not in fake.text
    assert app.sender.calls == []  # publishing is off: the gate never reaches Discord


@pytest.mark.parametrize("path", DEFERRED)
async def test_long_commands_defer_then_follow_up(path, app):
    app.members.append(Member(MEMBER_ID))

    fake = await run(app, path)

    kinds = [kind for kind, _, _ in fake.calls]
    assert kinds == ["defer", "followup"]
    assert fake.calls[0][2] == {"ephemeral": True, "thinking": True}
    assert_ephemeral(fake)


# --- each command -----------------------------------------------------------------------------


async def test_status_command_shows_gate_off(app):
    fake = await run(app, "status")

    assert "Publishing: OFF (previews only)" in fake.text


async def test_health_command(app):
    fake = await run(app, "health")

    assert "Publishing: OFF (previews only)" in fake.text
    assert "World state: not polled yet" in fake.text


async def test_version_command(app):
    fake = await run(app, "version")

    assert fake.text == f"Amir Beckett {__version__}"


async def test_config_validate_command(app):
    fake = await run(app, "config validate")

    assert fake.text == config_report(app.settings)


async def test_news_sources_command(app):
    await app.services.news.poll_all()

    fake = await run(app, "news sources")

    assert fake.text == sources_report(await snapshot(app.services, app.settings))


async def test_news_status_command(app):
    await app.services.news.poll_all()

    fake = await run(app, "news status")

    assert "Publishing: OFF (previews only)" in fake.text
    for source in app.settings.news.sources:
        assert f"`{source.id}`: 0 new" in fake.text


async def test_news_test_gated_uses_interaction_only(app):
    previews = PreviewRepo(app.db, app.clock)

    fake = await run(app, "news test")

    assert app.writer_calls == []  # the writer is never called...
    assert app.sender.calls == []  # ...so nothing can reach Discord
    assert await previews.count() == 0  # and no preview row is recorded either
    [(kind, _, kwargs)] = fake.calls
    assert kind == "send_message"
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    assert "Test" in embed.title
    assert embed.title == TEST_TITLE == "Test: not a real announcement"
    assert embed.footer.text.startswith("Amir Beckett · ")


async def test_news_test_with_publishing_on_goes_through_the_writer(make_app):
    app = await make_app(publishing=True)

    fake = await run(app, "news test")

    assert [call[:2] for call in app.writer_calls] == [("send", ChannelKey.NEWS)]
    [(kind, channel, cards, text, role_ping_id)] = app.sender.calls
    assert (kind, channel, text, role_ping_id) == ("send", ChannelKey.NEWS, None, None)
    assert [card.title for card in cards] == [TEST_TITLE]
    assert [kind for kind, _, _ in fake.calls] == ["defer", "followup"]
    assert all("embed" not in kwargs for _, kwargs in fake.replies)
    assert "posted to the news channel" in fake.text
    assert_ephemeral(fake)


async def test_news_check_respects_dedup(app, captured, monkeypatch):
    forum = next(s for s in app.settings.news.sources if s.kind == "forum_rss")
    items = parse_forum_rss(captured(FORUM_CAPTURE), forum.id).items
    newest = max(items, key=lambda item: item.published_at)
    # The baseline poll sees the feed without its newest topic; every later poll sees it all.
    app.fetcher.queued[forum.url] = [without_item(captured(FORUM_CAPTURE), newest.external_id)]
    await app.services.news.poll_all()
    polls = spy(monkeypatch, app.services.news, "poll_all")

    first = await run(app, "news check")
    second = await run(app, "news check")

    assert len(polls) == 2
    assert f"`{forum.id}`: 1 new, 0 baseline, 0 filtered" in first.text
    assert f"`{forum.id}`: 0 new, 0 baseline, 0 filtered" in second.text
    outbox = OutboxRepo(app.db, app.clock)
    assert await outbox.get_by_key(f"news:{forum.id}:{newest.external_id}") is not None
    assert (await outbox.counts_by_status())["pending"] == 1
    assert app.sender.calls == []


async def test_voice_status_command_counts_people_only(app):
    app.members.extend([Member(MEMBER_ID), Member(1, bot=True)])

    fake = await run(app, "voice status")

    assert "Voice: not in voice" in fake.text
    assert f"People in voice: <#{VOICE_ID}> 1" in fake.text
    assert str(MEMBER_ID) not in fake.text


async def test_reconnect_lifts_hold_and_pin_then_follows_the_crowd(app, caplog):
    s = app.services
    app.members.append(Member(MEMBER_ID))  # Warframe: 1
    app.elsewhere[PREDECESSOR_ID] += [Member(OTHER_MEMBER_ID), Member(THIRD_MEMBER_ID)]
    await s.session.ensure_connected(PREDECESSOR_ID)
    app.connector.channel_id = VOICE_ID  # a moderator drags him to Warframe: pinned there
    await s.companion.on_own_move(VOICE_ID)
    await s.companion.decided()
    await kick(app)  # then kicks him
    assert s.companion.pin is not None
    connects, disconnects = app.connector.connects, app.connector.disconnects

    with caplog.at_level(logging.INFO):
        fake = await run(app, "voice reconnect")

    assert fake.text == f"Amir has rejoined voice in <#{PREDECESSOR_ID}>."
    assert (s.companion.hold, s.companion.pin) == (None, None)
    assert await VoiceHoldRepo(app.db).get() is None
    # Straight to the biggest group: no leave and rejoin of its own, never back via Warframe.
    assert app.connector.targets[connects:] == [PREDECESSOR_ID]
    assert app.connector.disconnects == disconnects
    assert "voice hold cleared by an admin" in caplog.messages
    assert str(MEMBER_ID) not in caplog.text  # the admin is never named


async def test_reconnect_while_he_is_with_the_crowd_changes_nothing(app):
    app.members.append(Member(MEMBER_ID))
    await app.services.session.ensure_connected(VOICE_ID)

    fake = await run(app, "voice reconnect")

    assert fake.text == f"Amir has rejoined voice in <#{VOICE_ID}>."
    assert (app.connector.connects, app.connector.disconnects) == (1, 0)


async def test_reconnect_never_waits_out_a_settle_time(app):
    s = app.services
    app.members.append(Member(MEMBER_ID))
    await s.session.ensure_connected(VOICE_ID)
    app.elsewhere[PREDECESSOR_ID] += [Member(OTHER_MEMBER_ID), Member(THIRD_MEMBER_ID)]
    # Arcade goes ahead while Warframe still has someone: the 30 s settle time begins.
    await s.companion.on_event(VoiceEvent(VoiceEventKind.JOIN, THIRD_MEMBER_ID, PREDECESSOR_ID))
    await s.companion.decided()

    fake = await asyncio.wait_for(run(app, "voice reconnect"), 5)

    assert fake.text == f"Amir has rejoined voice in <#{VOICE_ID}>."  # where he is meanwhile
    assert app.connector.targets == [VOICE_ID]


async def test_reconnect_reports_a_failed_rejoin(app):
    app.members.append(Member(MEMBER_ID))
    await kick(app)
    app.connector.connect_error = VoiceConfigError("the channel is not a voice channel")

    fake = await run(app, "voice reconnect")

    assert app.connector.targets == [VOICE_ID]
    assert app.services.session.state == "failed"
    assert fake.text == "Amir could not rejoin voice; his log has the attempts."


async def test_reconnect_clears_a_hold_and_rejoins(app, caplog):
    holds = VoiceHoldRepo(app.db)
    app.members.append(Member(MEMBER_ID))
    await kick(app)
    assert await holds.get() is not None

    with caplog.at_level(logging.INFO):
        fake = await run(app, "voice reconnect")

    assert await holds.get() is None
    assert (app.connector.connects, app.services.session.state) == (1, "connected")
    assert fake.text == f"Amir has rejoined voice in <#{VOICE_ID}>."
    assert len(app.services.greetings) == 0  # quietly: nobody is greeted
    cleared = [r for r in caplog.records if r.getMessage() == "voice hold cleared by an admin"]
    assert [r.levelno for r in cleared] == [logging.INFO]
    assert str(MEMBER_ID) not in caplog.text  # the admin is never named


async def test_reconnect_during_a_kick_check_shows_the_hold(app):
    s = app.services
    app.members.append(Member(MEMBER_ID))
    await s.session.ensure_connected(VOICE_ID)
    app.kicks.kicked = True
    app.kicks.gate = asyncio.Event()  # the audit log read is held open
    app.connector.connected = False  # a moderator disconnects him
    check = asyncio.create_task(s.companion.on_own_disconnect())
    await asyncio.wait_for(app.kicks.asking.wait(), 1)
    # An admin runs the command while that kick check is still under way.
    command = asyncio.create_task(run(app, "voice reconnect"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not command.done()  # it waits for the kick check's answer
    app.kicks.gate.set()

    fake = await asyncio.wait_for(command, 5)
    await asyncio.wait_for(check, 5)

    hold = s.companion.hold
    assert hold is not None  # the kick wins
    assert fake.text.splitlines()[0] == (
        f"Voice: held out of voice since <t:{int(hold.since.timestamp())}:R> (kicked);"
        " use /amir voice reconnect"
    )
    assert app.connector.connects == 1  # no attempt to rejoin was made


async def test_reconnect_with_nobody_in_voice(app):
    holds = VoiceHoldRepo(app.db)
    # A hold stored when a drag still held him out: it holds until this command lifts it.
    await holds.save(VoiceHold(since=app.clock.now(), reason="moved"))
    await app.services.companion.load_hold()
    disconnects = app.connector.disconnects
    app.elsewhere[AFK_ID].append(Member(OTHER_MEMBER_ID))  # in voice, but nowhere he can join

    fake = await run(app, "voice reconnect")

    assert fake.replies[-1][0] == HOLD_CLEARED
    assert HOLD_CLEARED == (
        "Amir may join voice again. Nobody is in a channel he can join now, so he joins with the"
        " next arrival."
    )
    assert await holds.get() is None
    assert app.services.companion.hold is None
    assert (app.connector.connects, app.connector.disconnects) == (0, disconnects)

    app.elsewhere[PREDECESSOR_ID].append(Member(MEMBER_ID))  # the next arrival brings him in
    await app.services.companion.on_event(
        VoiceEvent(VoiceEventKind.JOIN, MEMBER_ID, PREDECESSOR_ID)
    )
    await app.services.companion.decided()
    assert app.connector.targets == [PREDECESSOR_ID]


async def test_reconnect_without_a_hold_while_nobody_is_in_voice(app):
    fake = await run(app, "voice reconnect")

    assert fake.replies[-1][0] == NOT_IN_VOICE
    assert NOT_IN_VOICE == "Amir only enters voice when someone is in a voice channel he can join."
    assert (app.connector.connects, app.connector.disconnects) == (0, 0)


@pytest.mark.parametrize("path", ["status", "voice status"])
async def test_status_commands_show_the_hold(path, app):
    app.members.append(Member(MEMBER_ID))
    app.kicks.kicked = True
    await app.services.companion.on_own_disconnect()  # a moderator's kick
    hold = app.services.companion.hold
    assert hold is not None

    fake = await run(app, path)

    unix = int(hold.since.timestamp())
    assert (
        f"Voice: held out of voice since <t:{unix}:R> (kicked); use /amir voice reconnect"
        in fake.text
    )


async def out_of_voice(app: App) -> None:
    app.elsewhere[PREDECESSOR_ID].append(Member(MEMBER_ID))  # people, but he has not joined


async def held_out(app: App) -> None:
    app.members.append(Member(MEMBER_ID))
    await app.services.session.ensure_connected(VOICE_ID)
    await kick(app)


async def leaving(app: App) -> None:
    await app.services.session.ensure_connected(VOICE_ID)
    app.services.session.schedule_disconnect()  # nobody is left: he leaves after the grace


@pytest.mark.parametrize("where", [out_of_voice, held_out, leaving])
async def test_voice_test_refuses_out_of_voice(where, app):
    await app.services.library.scan()
    await where(app)
    assert app.services.session.state != "connected"

    fake = await run(app, "voice test")

    assert fake.text == NOT_IN_VOICE
    assert len(app.services.greetings) == 0


async def test_voice_test_refuses_where_he_may_not_speak(app):
    s = app.services
    app.members.append(Member(MEMBER_ID))
    await s.library.scan()
    await s.session.ensure_connected(VOICE_ID)  # dragged there, say: a pin keeps him there
    app.silent.add(VOICE_ID)  # his role may not speak in it
    assert s.session.state == "connected"

    fake = await run(app, "voice test")

    assert fake.text == (
        f"Amir may not speak in <#{VOICE_ID}>, so a test greeting would not play there."
    )
    assert len(s.greetings) == 0  # nothing queued only to be dropped


async def test_voice_test_plays_in_any_channel(app, voice_clip):
    s = app.services
    app.elsewhere[PREDECESSOR_ID].append(Member(MEMBER_ID))
    await s.library.scan()
    await s.companion.on_startup()  # he joins the group in Arcade
    await s.companion.decided()

    fake = await run(app, "voice test")

    assert fake.text == f"A test greeting is queued to play in <#{PREDECESSOR_ID}>."
    worker = asyncio.create_task(s.greetings.run(s.companion.play_one))
    try:
        await asyncio.wait_for(app.player.finished.wait(), 5)
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
    assert [path.name for path in app.player.played] == [voice_clip.name]
    assert s.session.channel_id == PREDECESSOR_ID


async def test_voice_test_plays_one_greeting_and_never_announces(app, voice_clip):
    s = app.services
    app.members.append(Member(MEMBER_ID))
    await s.library.scan()
    await s.session.ensure_connected(VOICE_ID)

    first = await run(app, "voice test")
    second = await run(app, "voice test")

    assert "queued" in first.text
    assert "already" in second.text  # one test greeting at a time
    # The two workers the bot supervises: the greeting queue and the voice log.
    workers = [
        asyncio.create_task(s.greetings.run(s.companion.play_one)),
        asyncio.create_task(s.announcer.run()),
    ]
    try:
        await asyncio.wait_for(app.player.finished.wait(), 5)
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        for worker in workers:
            worker.cancel()
        for worker in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker
    assert [path.name for path in app.player.played] == [voice_clip.name]  # exactly one clip
    assert len(s.greetings) == 0
    assert app.writer_calls == []  # no voice-log line, sent or previewed
    assert await PreviewRepo(app.db, app.clock).count() == 0
    stays = s.companion._stays
    assert len(stays) == 0 and not stays._rejoin.is_rejoin(TEST_MEMBER_ID)  # id 0 left no trace


async def test_voice_test_refused_without_a_usable_clip(app):
    app.members.append(Member(MEMBER_ID))
    await app.services.session.ensure_connected(VOICE_ID)
    assert app.services.library.valid_count == 0  # not scanned: no clip is usable yet

    fake = await run(app, "voice test")

    assert fake.text == NO_CLIPS
    assert len(app.services.greetings) == 0
    assert_ephemeral(fake)


async def test_voice_greetings_rescans_and_reports_counts_only(app, voice_clip):
    broken = app.greetings_dir / f"broken{voice_clip.suffix}"
    broken.write_bytes(voice_clip.read_bytes()[:4000])  # a truncated copy fails validation

    fake = await run(app, "voice greetings")

    assert "1 valid, 1 invalid" in fake.text
    assert voice_clip.name not in fake.text and broken.name not in fake.text
    assert app.services.library.valid_count == 1


async def test_cycles_refresh_reports_each_world(app, monkeypatch):
    await app.poll_world()
    ensures = spy(monkeypatch, app.services.cycles, "ensure_cards")
    ticks = spy(monkeypatch, app.services.cycles, "tick")

    fake = await run(app, "cycles refresh")

    assert (len(ensures), len(ticks)) == (1, 1)
    for snap in app.services.cycles.current():
        assert f"<t:{int(snap.expiry.timestamp())}:R>" in fake.text
    assert app.sender.calls == []  # with publishing off, the cards stay previews


async def test_baro_next_command(app, captured):
    assert "No Baro Ki'Teer visit is known yet." in (await run(app, "baro next")).text

    await app.poll_world()
    fake = await run(app, "baro next")

    assert "Last call: <t:" in fake.text


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "target", "name", "kinds"),
    [
        ("status", "outbox_counts", None, ["send_message"]),
        ("news check", "news", "poll_all", ["defer", "followup"]),
    ],
)
async def test_command_error_becomes_a_polite_reply(
    path, target, name, kinds, app, monkeypatch, caplog
):
    async def broken(*args: Any) -> Any:
        raise RuntimeError(f"boom <@{MEMBER_ID}>")

    if target == "outbox_counts":
        monkeypatch.setattr(OutboxRepo, "counts_by_status", broken)
    else:
        monkeypatch.setattr(getattr(app.services, target), name, broken)

    with caplog.at_level(logging.ERROR, logger="amir.bot.commands"):
        fake = await run(app, path)

    assert [kind for kind, _, _ in fake.calls] == kinds
    assert "went wrong" in fake.text
    assert_ephemeral(fake)
    [record] = [r for r in caplog.records if r.name == "amir.bot.commands"]
    assert "RuntimeError" in record.getMessage()
    assert path in record.getMessage()
    assert str(MEMBER_ID) not in caplog.text and "boom" not in caplog.text
    assert record.exc_info is None


async def test_a_failed_apology_is_logged_not_raised(app, monkeypatch, caplog):
    async def broken(*args: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(OutboxRepo, "counts_by_status", broken)
    fake = FakeInteraction()
    fake.response.fail_with = RuntimeError("interaction expired")

    with caplog.at_level(logging.WARNING, logger="amir.bot.commands"):
        await command(build_amir_group(app.services, app.settings), "status").callback(fake)

    levels = [r.levelno for r in caplog.records if r.name == "amir.bot.commands"]
    assert levels == [logging.ERROR, logging.WARNING]


async def test_a_failed_refusal_is_logged_not_raised(app, caplog):
    fake = FakeInteraction(admin=False)
    fake.response.fail_with = RuntimeError(f"interaction expired <@{MEMBER_ID}>")

    with caplog.at_level(logging.WARNING, logger="amir.bot.commands"):
        await command(build_amir_group(app.services, app.settings), "status").callback(fake)

    records = [r for r in caplog.records if r.name == "amir.bot.commands"]
    assert [r.levelno for r in records] == [logging.ERROR, logging.WARNING]
    assert "amir status" in records[0].getMessage()
    assert "RuntimeError" in records[0].getMessage()
    assert str(MEMBER_ID) not in caplog.text and "expired" not in caplog.text
    assert fake.calls == []


# --- registration -----------------------------------------------------------------------------


async def test_register_amir_commands_adds_the_group_for_the_guild_only(app, monkeypatch):
    synced: list[Any] = []

    async def sync(*, guild=None):
        synced.append(guild)
        return []

    bot = app.bot
    bot._register_commands = register_amir_commands
    monkeypatch.setattr(bot.tree, "sync", sync)

    await bot.setup_hook()

    group = bot.tree.get_command("amir", guild=discord.Object(id=GUILD_ID))
    assert isinstance(group, app_commands.Group)
    assert group.guild_only is True
    assert bot.tree.get_command("amir") is None  # never registered globally
    assert [g.id for g in synced] == [GUILD_ID]
