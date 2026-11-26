"""The /amir slash commands: administrators only, in the one configured guild, every reply
ephemeral (seen only by the admin who asked).

Hard rule: no command posts, edits or deletes anything in a channel except
through the one GatedWriter (`services.writer`). An ephemeral interaction reply is not a channel
post. With publishing off, `news test` shows its card in that reply only and never calls the
writer; with publishing on, it posts through the writer like any other card.

Admin access is checked twice: Discord hides the group from non-administrators
(`default_permissions`, `guild_only`), and every callback checks again with `ensure_admin`
before doing anything. Each callback, that check included, is wrapped so that a failure becomes
a polite reply and a log line naming only the command and the exception type: no member data,
and no interaction is left unanswered. Commands that may take a while defer first, then reply
with a follow-up.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from amir import __version__, persona
from amir.bot.status import (
    baro_report,
    config_report,
    cycles_report,
    health_report,
    news_check_report,
    news_status_report,
    snapshot,
    sources_report,
    status_report,
    voice_report,
)
from amir.config.settings import Settings
from amir.news.cards import ANNOUNCEMENT_COLOR
from amir.publishing.cards import CONTENT, Card
from amir.publishing.gate import ChannelKey
from amir.publishing.sanitize import truncate
from amir.voice.queue import GreetingJob, OfferResult

if TYPE_CHECKING:
    from amir.bot.client import AmirBot, Services

log = logging.getLogger(__name__)

NOT_ADMIN = "Only this server's administrators may use Amir's commands."
NOT_IN_VOICE = "Amir only enters voice when someone is in a voice channel he can join."
HOLD_CLEARED = (
    "Amir may join voice again. Nobody is in a channel he can join now, so he joins with the"
    " next arrival."
)
NOT_REJOINED = "Amir could not rejoin voice; his log has the attempts."
CANNOT_SPEAK = "Amir may not speak in {channel}, so a test greeting would not play there."
FAILED = "Something went wrong with that command; the error type was logged."
NO_CLIPS = "No greeting clip is ready to play; /amir voice greetings checks them again."
TEST_TITLE = "Test: not a real announcement"
TEST_DESCRIPTION = "A test of the news card, nothing more. Nothing has been announced."
# The greeting job `voice test` queues: no member has id 0, and greetings are never announced.
TEST_MEMBER_ID = 0

_OFFERED = {
    OfferResult.ACCEPTED: "A test greeting is queued to play in {channel}.",
    OfferResult.DUPLICATE: "A test greeting is already waiting to play.",
    OfferResult.FULL: "The greeting queue is full; try again shortly.",
}

Body = Callable[[discord.Interaction], Awaitable[None]]


async def ensure_admin(interaction: discord.Interaction, guild_id: int) -> bool:
    """True for an administrator of the configured guild; anyone else gets a refusal."""
    permissions = getattr(interaction.user, "guild_permissions", None)
    if (
        interaction.guild_id == guild_id
        and permissions is not None
        and permissions.administrator is True
    ):
        return True
    log.info("an /amir command was refused: not an administrator of the configured server")
    await _reply(interaction, NOT_ADMIN)
    return False


def news_test_card() -> Card:
    """The card `news test` shows (publishing off) or posts (publishing on)."""
    return Card(
        title=TEST_TITLE,
        description=TEST_DESCRIPTION,
        color=ANNOUNCEMENT_COLOR,
        footer=persona.footer("Test card"),
    )


def build_amir_group(app: Services, settings: Settings) -> app_commands.Group:
    amir = app_commands.Group(
        name="amir",
        description="Amir Beckett's admin commands",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )
    voice = app_commands.Group(name="voice", description="Amir's voice companion", parent=amir)
    news = app_commands.Group(name="news", description="Warframe news", parent=amir)
    config = app_commands.Group(name="config", description="Amir's configuration", parent=amir)
    cycles = app_commands.Group(name="cycles", description="Open-world cycles", parent=amir)
    baro = app_commands.Group(name="baro", description="Baro Ki'Teer", parent=amir)

    c = _Commands(app, settings)
    guild_id = settings.discord.guild_id
    for group, name, description, body in [
        (amir, "status", "What Amir is doing, in brief", c.status),
        (amir, "health", "How each part of Amir is running", c.health),
        (amir, "version", "Amir's version", c.version),
        (voice, "status", "Which voice channel Amir is in, and why", c.voice_status),
        (
            voice,
            "reconnect",
            "Bring Amir back after a kick, or free him from a drag: he follows the crowd",
            c.voice_reconnect,
        ),
        (voice, "test", "Play a test greeting", c.voice_test),
        (voice, "greetings", "Re-check the greeting clips", c.voice_greetings),
        (news, "status", "Each news source's latest poll", c.news_status),
        (news, "check", "Poll every news source now", c.news_check),
        (news, "sources", "The configured news sources", c.news_sources),
        (news, "test", "Show a test news card", c.news_test),
        (config, "validate", "Check the running configuration", c.config_validate),
        (cycles, "refresh", "Refresh the cycle cards now", c.cycles_refresh),
        (baro, "next", "Baro Ki'Teer's next milestones", c.baro_next),
    ]:
        _add(group, name, description, guild_id, body)
    return amir


async def register_amir_commands(bot: AmirBot) -> None:
    """AmirBot's `register_commands`: the /amir group, for the configured guild only."""
    group = build_amir_group(bot.services, bot.settings)
    bot.tree.add_command(group, guild=discord.Object(id=bot.settings.discord.guild_id))


def _add(group: app_commands.Group, name: str, description: str, guild_id: int, body: Body) -> None:
    label = f"{group.qualified_name} {name}"

    async def callback(interaction: discord.Interaction) -> None:
        try:
            if not await ensure_admin(interaction, guild_id):
                return
            await body(interaction)
        except Exception as exc:
            # The type only: a message could carry member data.
            log.error("/%s failed: %s", label, type(exc).__name__)
            try:
                await _reply(interaction, FAILED)
            except Exception as reply_exc:
                log.warning(
                    "/%s: the failure reply failed too: %s", label, type(reply_exc).__name__
                )

    group.command(name=name, description=description)(callback)


class _Commands:
    def __init__(self, app: Services, settings: Settings) -> None:
        self._app = app
        self._settings = settings

    async def _report(self, interaction: discord.Interaction, build: Callable[..., str]) -> None:
        await _reply(interaction, build(await snapshot(self._app, self._settings)))

    async def status(self, interaction: discord.Interaction) -> None:
        await self._report(interaction, status_report)

    async def health(self, interaction: discord.Interaction) -> None:
        await self._report(interaction, health_report)

    async def version(self, interaction: discord.Interaction) -> None:
        await _reply(interaction, f"Amir Beckett {__version__}")

    async def voice_status(self, interaction: discord.Interaction) -> None:
        await self._report(interaction, voice_report)

    async def voice_reconnect(self, interaction: discord.Interaction) -> None:
        """Lift a kick's hold and a drag's pin, then let voice-follow take Amir to the crowd.
        Never a leave and rejoin of its own: that would bounce him through a channel the crowd
        has left. The reply waits for the follow decision (a connect included, bounded by the
        session's backoff), but not for a settle time: meanwhile he is where the reply says. A
        kick check still under way when the command runs may find a kick and hold him out again;
        the reply is then the voice status, which shows that hold."""
        await _defer(interaction)
        companion = self._app.companion
        released = await companion.release_hold()
        await companion.decided()
        channel_id = self._app.session.channel_id
        if companion.hold is not None:
            await self._report(interaction, voice_report)
        elif companion.humans_present() == 0:
            await _reply(interaction, HOLD_CLEARED if released else NOT_IN_VOICE)
        elif channel_id is None:
            await _reply(interaction, NOT_REJOINED)
        else:
            await _reply(interaction, f"Amir has rejoined voice in <#{channel_id}>.")

    async def voice_test(self, interaction: discord.Interaction) -> None:
        """Queue a test greeting where Amir is, whichever voice channel that is, unless it would
        never play: out of voice, where his role may not speak, or with no clip."""
        session = self._app.session
        channel_id = session.channel_id
        if session.state != "connected" or channel_id is None:
            await _reply(interaction, NOT_IN_VOICE)
            return
        channel = f"<#{channel_id}>"
        if not self._app.companion.voice_census().can_speak_in(channel_id):
            await _reply(interaction, CANNOT_SPEAK.format(channel=channel))
            return
        if self._app.library.valid_count == 0:
            await _reply(interaction, NO_CLIPS)  # a queued greeting would never play
            return
        queue = self._app.greetings
        result = queue.offer(GreetingJob(TEST_MEMBER_ID, queue.clock()))
        await _reply(interaction, _OFFERED[result].format(channel=channel))

    async def voice_greetings(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        report = await self._app.library.scan()
        await _reply(
            interaction,
            f"Greeting clips: {len(report.valid)} valid, {len(report.invalid)} invalid.",
        )

    async def news_status(self, interaction: discord.Interaction) -> None:
        await self._report(interaction, news_status_report)

    async def news_check(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        await _reply(interaction, news_check_report(await self._app.news.poll_all()))

    async def news_sources(self, interaction: discord.Interaction) -> None:
        await self._report(interaction, sources_report)

    async def news_test(self, interaction: discord.Interaction) -> None:
        card = news_test_card()
        if not self._app.writer.enabled:
            # Publishing off: the card is shown in this reply only; the writer is never called.
            await _reply(
                interaction,
                "Publishing is off, so this test card is shown only to you; nothing was posted.",
                embed=discord.Embed.from_dict(card.to_dict()),
            )
            return
        await _defer(interaction)
        await self._app.writer.send(ChannelKey.NEWS, cards=[card])
        await _reply(interaction, "The test card was posted to the news channel.")

    async def config_validate(self, interaction: discord.Interaction) -> None:
        await _reply(interaction, config_report(self._settings))

    async def cycles_refresh(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        await self._app.cycles.ensure_cards()
        await self._app.cycles.tick()
        await self._report(interaction, cycles_report)

    async def baro_next(self, interaction: discord.Interaction) -> None:
        await self._report(interaction, baro_report)


async def _defer(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)


async def _reply(
    interaction: discord.Interaction, content: str, *, embed: discord.Embed | None = None
) -> None:
    """Answer ephemerally: the response if it is still open, else a follow-up to the defer."""
    kwargs: dict[str, Any] = {
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if embed is not None:
        kwargs["embed"] = embed
    text = truncate(content, CONTENT)
    if interaction.response.is_done():
        await interaction.followup.send(text, **kwargs)
    else:
        await interaction.response.send_message(text, **kwargs)
