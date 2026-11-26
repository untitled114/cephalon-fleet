"""Admin reports: plain-text replies built by pure functions from a frozen StatusView.

`snapshot` reads the running services once (the database included) into a StatusView; every
report below is a pure function of a view or of the settings. Times are Discord timestamps,
which each reader's client shows in their own timezone. No report carries the token, an
environment value, a filesystem path, a URL's query string, or a member's id or name; text that
comes from the config or a feed is escaped so it can neither ping nor break the formatting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from amir import __version__
from amir.baro.schedule import Milestone, milestone_times
from amir.config.settings import Settings
from amir.cycles.model import NEXT_STATE, WORLD_NAMES
from amir.news.pipeline import PollReport
from amir.persona import cycle_change_line
from amir.publishing.sanitize import escape_markdown, neutralise_mentions, truncate
from amir.storage.repos import OutboxRepo, SourceStateRepo, VoiceHold
from amir.voice.companion import Pin
from amir.worldstate.poller import PollSummary
from amir.worldstate.wfcd import CycleSnapshot

if TYPE_CHECKING:
    from amir.bot.client import Services

PUBLISHING_OFF = "Publishing: OFF (previews only)"
PUBLISHING_ON = "Publishing: ON"
MILESTONE_LABELS = {
    Milestone.WARNING_72H: "Advance notice",
    Milestone.ARRIVAL: "Arrival",
    Milestone.DEPARTURE_10H: "Last call",
}
# Where Amir is, by voice session state, when there is no channel to name (see _voice).
_VOICE_STATES = {
    "idle": "not in voice",
    "connecting": "joining a voice channel",
    "connected": "in voice",
    "grace": "in voice, leaving shortly",
    "failed": "not in voice: the last attempts to join failed",
}
_TEXT_LIMIT = 150


@dataclass(frozen=True)
class SourceView:
    """One configured news source: its settings (no URL, only the host) and polling state."""

    id: str
    label: str
    kind: str
    host: str
    enabled: bool
    interval_seconds: int
    last_success_at: datetime | None
    last_error: str | None
    last_poll: PollReport | None  # the latest poll since startup


@dataclass(frozen=True)
class StatusView:
    version: str
    now: datetime
    publishing_enabled: bool
    voice_state: str
    voice_channel_id: int | None  # the voice channel Amir is in
    voice_pin: Pin | None  # a drag keeps Amir in its channel while anyone is in it
    voice_hold: VoiceHold | None  # a moderator's kick keeps Amir out of voice
    people_by_channel: tuple[tuple[int, int], ...]  # (channel, people) with people, server order
    greetings_valid: int
    sources: tuple[SourceView, ...]
    outbox: tuple[tuple[str, int], ...]  # (status, rows) for every status, in lifecycle order
    worldstate: PollSummary | None  # the latest world-state poll
    baro_milestones: tuple[tuple[Milestone, datetime], ...]  # Baro's latest known visit
    cycles: tuple[CycleSnapshot, ...]
    next_cycle_change: datetime | None
    cycles_failing: tuple[tuple[str, str, datetime], ...]  # (world, error type, next try)
    tasks_running: tuple[str, ...]

    @property
    def next_baro(self) -> tuple[Milestone, datetime] | None:
        """The first milestone of Baro's latest known visit still ahead, if any."""
        return next(((m, at) for m, at in self.baro_milestones if at > self.now), None)


async def snapshot(services: Services, settings: Settings) -> StatusView:
    """Read the running services into a StatusView."""
    states = SourceStateRepo(services.db)
    sources = []
    for src in settings.news.sources:
        state = await states.get(src.id)
        sources.append(
            SourceView(
                id=src.id,
                label=src.label,
                kind=src.kind,
                host=_host(src.url),
                enabled=src.enabled,
                interval_seconds=src.interval_seconds,
                last_success_at=state.last_success_at,
                last_error=state.last_error,
                last_poll=services.news.last_reports.get(src.id),
            )
        )
    outbox = await OutboxRepo(services.db).counts_by_status()
    visit = services.worldstate.last_visit
    census = services.companion.voice_census()
    milestones: tuple[tuple[Milestone, datetime], ...] = ()
    if visit is not None:
        times = milestone_times(
            visit,
            warn_hours=settings.baro.warn_before_arrival_hours,
            depart_hours=settings.baro.warn_before_departure_hours,
        )
        milestones = tuple(sorted(times.items(), key=lambda item: item[1]))
    return StatusView(
        version=__version__,
        now=services.clock.now(),
        publishing_enabled=services.writer.enabled,
        voice_state=services.session.state,
        voice_channel_id=services.session.channel_id,
        voice_pin=services.companion.pin,
        voice_hold=services.companion.hold,
        people_by_channel=tuple(
            (view.channel_id, len(view.humans)) for view in census.channels if view.humans
        ),
        greetings_valid=services.library.valid_count,
        sources=tuple(sources),
        outbox=tuple(outbox.items()),
        worldstate=services.worldstate.last_summary,
        baro_milestones=milestones,
        cycles=tuple(services.cycles.current()),
        next_cycle_change=services.cycles.next_change_at(),
        cycles_failing=tuple(
            (world, error, retry_at)
            for world, (error, retry_at) in services.cycles.failing().items()
        ),
        tasks_running=tuple(services.supervisor.running()),
    )


# --- reports ---------------------------------------------------------------------------------


def status_report(view: StatusView) -> str:
    enabled = sum(1 for s in view.sources if s.enabled)
    failing = sum(1 for s in view.sources if s.enabled and s.last_error)
    next_baro = view.next_baro
    baro = "none known"
    if next_baro is not None:
        baro = f"{MILESTONE_LABELS[next_baro[0]]} {_when(next_baro[1])}"
    cycle = "not known yet" if view.next_cycle_change is None else _when(view.next_cycle_change)
    return "\n".join(
        [
            f"**Amir Beckett {view.version}**",
            _publishing(view.publishing_enabled),
            f"Voice: {_voice(view)} · {_count(view.greetings_valid, 'greeting clip')} ready",
            f"News: {enabled} of {len(view.sources)} sources enabled, {failing} failing",
            _outbox_line(view),
            f"Next Baro milestone: {baro}",
            f"Next cycle change: {cycle}",
        ]
    )


def health_report(view: StatusView) -> str:
    tasks = ", ".join(view.tasks_running) if view.tasks_running else "none running"
    failing = [s.id for s in view.sources if s.enabled and s.last_error]
    enabled = sum(1 for s in view.sources if s.enabled)
    news = f"News sources: {len(failing)} of {enabled} failing"
    if failing:
        news += " (" + ", ".join(_code(s) for s in failing) + ")"
    outbox = dict(view.outbox)
    return "\n".join(
        [
            "**Amir Beckett health**",
            _publishing(view.publishing_enabled),
            f"Background tasks: {tasks}",
            f"World state: {_worldstate(view.worldstate)}",
            _cycle_failures(view),
            news,
            f"Outbox: {outbox.get('failed', 0)} failed, {outbox.get('pending', 0)} pending",
            f"Voice: {_voice(view)} · {_count(view.greetings_valid, 'greeting clip')} ready",
        ]
    )


def config_report(settings: Settings) -> str:
    """What `--check-config` prints and `/amir config validate` replies: never the token, an
    environment value or a path."""
    missing = settings.missing_runtime_ids()
    sources = [f"{s.id} ({'on' if s.enabled else 'off'})" for s in settings.news.sources]
    return "\n".join(
        [
            f"Amir Beckett {__version__}: the configuration is valid",
            _publishing(settings.publishing.enabled),
            "Missing runtime IDs: " + (", ".join(missing) if missing else "none"),
            "News sources: " + (", ".join(sources) if sources else "none"),
        ]
    )


def sources_report(view: StatusView) -> str:
    if not view.sources:
        return "**News sources**\nNo news source is configured."
    lines = ["**News sources**"]
    for s in view.sources:
        lines += [
            f"• {_code(s.id)} ({'on' if s.enabled else 'off'}): {_plain(s.label)}",
            f"  {s.kind} from {_plain(s.host) or 'an unreadable URL'},"
            f" every {_interval(s.interval_seconds)}",
            "  Last success: "
            + ("never" if s.last_success_at is None else _when(s.last_success_at))
            + " · Last error: "
            + ("none" if not s.last_error else _plain(s.last_error)),
        ]
    return "\n".join(lines)


def news_status_report(view: StatusView) -> str:
    lines = ["**News**", _publishing(view.publishing_enabled), _outbox_line(view)]
    for s in view.sources:
        if not s.enabled:
            lines.append(f"• {_code(s.id)}: disabled")
            continue
        last = "not polled since startup" if s.last_poll is None else _poll_text(s.last_poll)
        success = "never" if s.last_success_at is None else f"<t:{_unix(s.last_success_at)}:R>"
        lines.append(f"• {_code(s.id)}: {last} · last success {success}")
    return "\n".join(lines)


def news_check_report(reports: Mapping[str, PollReport]) -> str:
    """The outcome of polling every enabled source once, as `/amir news check` does."""
    if not reports:
        return "**News check**\nNo news source is enabled."
    lines = ["**News check**"]
    lines += [f"• {_code(sid)}: {_poll_text(report)}" for sid, report in reports.items()]
    return "\n".join(lines)


def voice_report(view: StatusView) -> str:
    """Where Amir is in voice and why, and how many people are in each voice channel: counts
    only, never who."""
    people = " · ".join(f"<#{channel}> {count}" for channel, count in view.people_by_channel)
    return "\n".join(
        [
            f"Voice: {_voice(view)}",
            f"People in voice: {people or 'nobody'}",
            f"Greeting clips ready: {view.greetings_valid}",
        ]
    )


def cycles_report(view: StatusView) -> str:
    if not view.cycles:
        return "**Open-world cycles**\nNo cycle data yet: the world state has not been read."
    lines = ["**Open-world cycles**"]
    failing = {world: (error, at) for world, error, at in view.cycles_failing}
    for snap in view.cycles:
        unix = _unix(snap.expiry)
        change = cycle_change_line(NEXT_STATE[snap.state], f"<t:{unix}:R>", f"<t:{unix}:t>")
        lines.append(f"• {WORLD_NAMES.get(snap.world, snap.world)}: {snap.state}. {change}")
        if snap.world in failing:
            error, at = failing[snap.world]
            lines.append(f"  Card not updating: {_code(error)}; next try <t:{_unix(at)}:R>")
    return "\n".join(lines)


def baro_report(view: StatusView) -> str:
    if not view.baro_milestones:
        return "No Baro Ki'Teer visit is known yet."
    ahead = [(m, at) for m, at in view.baro_milestones if at > view.now]
    if not ahead:
        return "Baro Ki'Teer's last known visit is over; his next is not known yet."
    lines = ["**Baro Ki'Teer's next milestones**"]
    lines += [f"• {MILESTONE_LABELS[m]}: {_when(at)}" for m, at in ahead]
    return "\n".join(lines)


# --- helpers ---------------------------------------------------------------------------------


def _publishing(enabled: bool) -> str:
    return PUBLISHING_ON if enabled else PUBLISHING_OFF


def _voice(view: StatusView) -> str:
    """Which voice channel Amir is in and why: with the most people, or pinned there by a drag.
    While a moderator holds him out, that and how to end it."""
    hold = view.voice_hold
    if hold is not None:
        return (
            f"held out of voice since <t:{_unix(hold.since)}:R> ({hold.reason});"
            " use /amir voice reconnect"
        )
    channel = view.voice_channel_id
    if channel is None or view.voice_state not in ("connected", "grace"):
        return _VOICE_STATES.get(view.voice_state, view.voice_state)
    if view.voice_state == "grace":
        return f"in <#{channel}>, leaving shortly"
    pin = view.voice_pin
    if pin is not None and pin.channel_id == channel:
        return f"pinned in <#{channel}> by a drag since <t:{_unix(pin.since)}:R>"
    return f"in <#{channel}>, with the most people"


def _outbox_line(view: StatusView) -> str:
    return "Outbox: " + ", ".join(f"{rows} {status}" for status, rows in view.outbox)


def _cycle_failures(view: StatusView) -> str:
    if not view.cycles_failing:
        return "Cycle cards: no failures"
    failures = [
        f"{WORLD_NAMES.get(world, world)} ({_code(error)}, next try <t:{_unix(at)}:R>)"
        for world, error, at in view.cycles_failing
    ]
    return "Cycle cards: failing for " + ", ".join(failures)


def _worldstate(summary: PollSummary | None) -> str:
    if summary is None:
        return "not polled yet"
    official = "ok" if summary.official_ok else "failing"
    wfcd = "ok" if summary.wfcd_ok else "failing"
    return (
        f"official {official}, WFCD {wfcd}, {_count(len(summary.errors), 'error')} in the last poll"
    )


def _poll_text(report: PollReport) -> str:
    if report.error is not None:
        return f"error: {_plain(report.error)}"
    if report.not_modified:
        return "not modified"
    return f"{report.new} new, {report.baseline} baseline, {report.filtered} filtered"


def _host(url: str) -> str:
    """Only the host of a source URL: never its path or query string."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _plain(text: str) -> str:
    """Config or feed text, made safe for a reply: no pings, no markdown, bounded length."""
    return truncate(neutralise_mentions(escape_markdown(text)), _TEXT_LIMIT)


def _code(text: str) -> str:
    """An identifier as inline code; nothing inside a code span pings or formats."""
    return "`" + truncate(text.replace("`", "'"), _TEXT_LIMIT) + "`"


def _unix(dt: datetime) -> int:
    return int(dt.timestamp())


def _when(dt: datetime) -> str:
    unix = _unix(dt)
    return f"<t:{unix}:f> (<t:{unix}:R>)"


def _interval(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"
