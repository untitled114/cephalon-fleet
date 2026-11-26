import asyncio
import re
from datetime import timedelta
from urllib.parse import urlsplit

from amir import __version__
from amir.baro.schedule import Milestone
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
from amir.cycles.model import WORLD_NAMES, roll_forward
from amir.cycles.service import RETRY_BASE_SECONDS
from amir.news.forum_rss import parse_forum_rss
from amir.news.pipeline import PollReport
from amir.publishing.gate import PermissionDenied
from amir.storage.repos import VoiceHold, VoiceHoldRepo
from amir.worldstate.official import parse_void_trader
from amir.worldstate.poller import PollSummary
from amir.worldstate.wfcd import parse_cycles

from .harness import (
    AFK_ID,
    FORUM_CAPTURE,
    MEMBER_ID,
    OFFICIAL_CAPTURE,
    OTHER_MEMBER_ID,
    PREDECESSOR_ID,
    STEAM_CAPTURE,
    THIRD_MEMBER_ID,
    TOKEN,
    VOICE_ID,
    WFCD_CAPTURE,
    App,
    Member,
    make_settings,
)

STATUSES = ("pending", "sending", "sent", "previewed", "failed")


def unix(dt) -> int:
    return int(dt.timestamp())


def rolled_capture(captured, app: App):
    """The captured cycles rolled to the harness clock, in configured world order."""
    snaps = parse_cycles(captured(WFCD_CAPTURE))
    durations = app.settings.cycles.durations
    return [
        roll_forward(snaps[world], app.clock.now(), durations)
        for world in app.settings.cycles.worlds
    ]


def last_call(captured):
    visit = parse_void_trader(captured(OFFICIAL_CAPTURE))
    return visit.expiry - timedelta(hours=10)


async def all_reports(app: App, poll_reports=None) -> dict[str, str]:
    view = await snapshot(app.services, app.settings)
    return {
        "status": status_report(view),
        "health": health_report(view),
        "sources": sources_report(view),
        "news status": news_status_report(view),
        "voice": voice_report(view),
        "cycles": cycles_report(view),
        "baro": baro_report(view),
        "config": config_report(app.settings),
        "news check": news_check_report(poll_reports or {}),
    }


# --- the snapshot ----------------------------------------------------------------------------


async def test_snapshot_before_anything_ran(app):
    view = await snapshot(app.services, app.settings)

    assert view.version == __version__
    assert view.now == app.clock.now()
    assert view.publishing_enabled is False
    assert view.voice_state == "idle"
    assert view.voice_hold is None
    assert view.voice_channel_id is None
    assert view.voice_pin is None
    assert view.people_by_channel == ()
    assert view.greetings_valid == 0  # nothing scanned yet
    assert [s.id for s in view.sources] == [s.id for s in app.settings.news.sources]
    assert all(s.last_success_at is None and s.last_error is None for s in view.sources)
    assert all(s.last_poll is None for s in view.sources)
    assert dict(view.outbox) == dict.fromkeys(STATUSES, 0)
    assert view.worldstate is None
    assert view.baro_milestones == ()
    assert view.next_baro is None
    assert view.cycles == ()
    assert view.next_cycle_change is None
    assert view.cycles_failing == ()
    assert view.tasks_running == ()


async def test_snapshot_reads_the_running_services(app, captured):
    await app.services.library.scan()
    await app.poll_world()
    await app.services.news.poll_all()
    app.members.extend([Member(MEMBER_ID), Member(OTHER_MEMBER_ID), Member(1, bot=True)])

    view = await snapshot(app.services, app.settings)

    assert view.greetings_valid == 1  # the test clip, validated by real FFmpeg
    assert view.people_by_channel == ((VOICE_ID, 2),)  # the bot is not counted
    for source in view.sources:
        configured = next(s for s in app.settings.news.sources if s.id == source.id)
        assert (source.label, source.kind) == (configured.label, configured.kind)
        assert source.host == urlsplit(configured.url).hostname
        assert (source.enabled, source.interval_seconds) == (True, configured.interval_seconds)
        assert source.last_success_at == app.clock.now()
        assert source.last_error is None
        assert source.last_poll is not None and source.last_poll.baseline > 0  # first poll
    # Baro is present at the harness time: his arrival row is queued, nothing has drained it.
    assert dict(view.outbox) == {**dict.fromkeys(STATUSES, 0), "pending": 1}
    assert view.worldstate == PollSummary(True, True, True, [])
    visit = parse_void_trader(captured(OFFICIAL_CAPTURE))
    assert [m for m, _ in view.baro_milestones] == list(Milestone)
    assert dict(view.baro_milestones)[Milestone.ARRIVAL] == visit.activation
    assert view.next_baro == (Milestone.DEPARTURE_10H, last_call(captured))
    expected = rolled_capture(captured, app)
    assert list(view.cycles) == expected
    assert view.next_cycle_change == min(s.expiry for s in expected)


async def test_snapshot_lists_the_running_tasks(app):
    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await asyncio.Event().wait()

    app.services.supervisor.start("news", worker)
    await started.wait()

    view = await snapshot(app.services, app.settings)

    assert view.tasks_running == ("news",)
    assert "news" in health_report(view)


# --- what the reports say --------------------------------------------------------------------


async def test_status_shows_gate_off(app):
    report = status_report(await snapshot(app.services, app.settings))

    assert "Publishing: OFF (previews only)" in report
    assert "Publishing: ON" not in report


async def test_status_shows_gate_on(make_app):
    app = await make_app(publishing=True)

    report = status_report(await snapshot(app.services, app.settings))

    assert "Publishing: ON" in report
    assert "OFF" not in report


async def test_reports_leak_nothing(app, captured, voice_clip):
    # A rich view: clips scanned (one of them broken), the world state read, both sources
    # baselined, then the forum source failing on a body that is not a feed.
    broken = app.greetings_dir / f"broken{voice_clip.suffix}"
    broken.write_bytes(voice_clip.read_bytes()[:4000])
    await app.services.library.scan()
    await app.poll_world()
    await app.services.news.poll_all()
    forum = next(s for s in app.settings.news.sources if s.kind == "forum_rss")
    app.fetcher.queued[forum.url] = [captured(STEAM_CAPTURE)]
    poll_reports = await app.services.news.poll_all()
    assert poll_reports[forum.id].error is not None
    app.members.append(Member(MEMBER_ID))

    reports = await all_reports(app, poll_reports)

    forbidden = [
        TOKEN,
        "DISCORD_TOKEN=",
        "db_path",
        "heartbeat_path",
        "greetings_dir",
        "/data",
        "/app",
        str(app.greetings_dir.parents[2]),  # the harness's tmp root
        voice_clip.name,
        broken.name,
        str(MEMBER_ID),
    ]
    forbidden += [urlsplit(s.url).query for s in app.settings.news.sources if urlsplit(s.url).query]
    for name, report in reports.items():
        leaks = [text for text in forbidden if text in report]
        assert leaks == [], name


async def test_times_are_discord_timestamps(app, captured):
    await app.poll_world()
    await app.services.news.poll_all()
    view = await snapshot(app.services, app.settings)

    status = status_report(view)

    assert f"<t:{unix(view.next_cycle_change)}:R>" in status
    assert f"<t:{unix(last_call(captured))}:R>" in status
    assert f"<t:{unix(app.clock.now())}:R>" in sources_report(view)  # last success
    iso_date = re.compile(r"\d{4}-\d{2}-\d{2}")
    for name, report in (await all_reports(app)).items():
        assert not iso_date.search(report), name


async def test_sources_report_lists_the_configured_sources(make_app, tmp_path, ffmpeg_exe):
    base = make_settings(tmp_path / "custom", ffmpeg_exe)
    steam = next(s for s in base.news.sources if s.kind == "steam_news")
    sources = [
        s if s is not steam else s.model_copy(update={"enabled": False}) for s in base.news.sources
    ]
    settings = base.model_copy(update={"news": base.news.model_copy(update={"sources": sources})})
    app = await make_app(settings=settings)
    await app.services.news.poll_all()  # polls the enabled forum source only

    report = sources_report(await snapshot(app.services, app.settings))

    for source in settings.news.sources:
        assert f"`{source.id}`" in report
        assert source.label in report
        assert urlsplit(source.url).hostname in report
    assert "(on)" in report and "(off)" in report
    assert "every 5 min" in report and "every 15 min" in report
    assert "never" in report  # the disabled source never succeeded
    status = news_status_report(await snapshot(app.services, app.settings))
    assert f"`{steam.id}`: disabled" in status


async def test_reports_without_news_sources(make_app, tmp_path, ffmpeg_exe):
    base = make_settings(tmp_path / "custom", ffmpeg_exe)
    settings = base.model_copy(update={"news": base.news.model_copy(update={"sources": []})})
    app = await make_app(settings=settings)

    view = await snapshot(app.services, app.settings)

    assert "No news source is configured." in sources_report(view)
    assert "No news source is enabled." in news_check_report(await app.services.news.poll_all())
    assert "News: 0 of 0 sources enabled, 0 failing" in status_report(view)


async def test_an_unreadable_source_url_shows_no_host(make_app, tmp_path, ffmpeg_exe):
    base = make_settings(tmp_path / "custom", ffmpeg_exe)
    forum = next(s for s in base.news.sources if s.kind == "forum_rss")
    broken = forum.model_copy(update={"url": forum.url.replace("://", "://[", 1)})
    sources = [broken if s is forum else s for s in base.news.sources]
    settings = base.model_copy(update={"news": base.news.model_copy(update={"sources": sources})})
    app = await make_app(settings=settings)

    view = await snapshot(app.services, app.settings)
    report = sources_report(view)

    assert next(s for s in view.sources if s.id == forum.id).host == ""
    assert "forum_rss from an unreadable URL" in report
    assert urlsplit(forum.url).path not in report


async def test_news_status_report_shows_each_last_poll(app, captured):
    report = news_status_report(await snapshot(app.services, app.settings))
    assert "Publishing: OFF (previews only)" in report
    assert "not polled since startup" in report

    await app.services.news.poll_all()
    view = await snapshot(app.services, app.settings)
    report = news_status_report(view)

    for source in view.sources:
        assert f"`{source.id}`: 0 new, {source.last_poll.baseline} baseline, 0 filtered" in report
    assert "Outbox: 0 pending, 0 sending, 0 sent, 0 previewed, 0 failed" in report


def test_news_check_report_counts_errors_and_not_modified():
    report = news_check_report(
        {
            "a": PollReport(new=2, baseline=0, filtered=1),
            "b": PollReport(error="not a usable feed: missing appnews.newsitems"),
            "c": PollReport(not_modified=True),
        }
    )

    assert "`a`: 2 new, 0 baseline, 1 filtered" in report
    assert "`b`: error: not a usable feed: missing appnews.newsitems" in report
    assert "`c`: not modified" in report
    assert "No news source is enabled" in news_check_report({})


async def test_news_check_report_from_a_real_poll(app, captured):
    reports = await app.services.news.poll_all()

    report = news_check_report(reports)

    items = parse_forum_rss(captured(FORUM_CAPTURE), "forum").items
    forum = next(s for s in app.settings.news.sources if s.kind == "forum_rss")
    assert f"`{forum.id}`: 0 new, {len(items)} baseline, 0 filtered" in report


def voice_lines(view) -> list[str]:
    """The voice line of each report that has one: /amir voice status, /amir status and
    /amir health."""
    return [
        voice_report(view).splitlines()[0],
        next(line for line in status_report(view).splitlines() if line.startswith("Voice: ")),
        next(line for line in health_report(view).splitlines() if line.startswith("Voice: ")),
    ]


async def test_voice_report_names_his_channel_and_why(app):
    s = app.services
    await s.library.scan()
    app.members.append(Member(MEMBER_ID))  # Warframe: 1
    app.elsewhere[PREDECESSOR_ID] += [Member(OTHER_MEMBER_ID), Member(THIRD_MEMBER_ID)]
    ready = " · 1 greeting clip ready"

    await s.companion.on_startup()  # he joins the biggest group: Arcade
    await s.companion.decided()
    most = f"Voice: in <#{PREDECESSOR_ID}>, with the most people"
    assert voice_lines(await snapshot(s, app.settings)) == [most, most + ready, most + ready]

    dragged_at = app.clock.now()
    app.connector.channel_id = VOICE_ID  # a moderator drags him to Warframe
    await s.companion.on_own_move(VOICE_ID)
    await s.companion.decided()
    app.clock.advance(300)
    view = await snapshot(s, app.settings)
    assert view.voice_channel_id == VOICE_ID
    assert view.voice_pin is not None and view.voice_pin.channel_id == VOICE_ID
    pinned = f"Voice: pinned in <#{VOICE_ID}> by a drag since <t:{unix(dragged_at)}:R>"
    assert voice_lines(view) == [pinned, pinned + ready, pinned + ready]

    app.kicks.kicked = True
    app.connector.connected = False  # a moderator disconnects him
    await s.companion.on_own_disconnect()
    held = (
        f"Voice: held out of voice since <t:{unix(app.clock.now())}:R> (kicked);"
        " use /amir voice reconnect"
    )
    assert voice_lines(await snapshot(s, app.settings)) == [held, held + ready, held + ready]


async def test_voice_report_counts_people_by_channel_without_names(app):
    app.members.extend([Member(MEMBER_ID), Member(OTHER_MEMBER_ID), Member(1, bot=True)])
    app.elsewhere[AFK_ID].append(Member(THIRD_MEMBER_ID))  # parked in AFK; Arcade is empty

    view = await snapshot(app.services, app.settings)

    assert view.people_by_channel == ((VOICE_ID, 2), (AFK_ID, 1))  # in server order
    report = voice_report(view)
    assert report.splitlines()[1] == f"People in voice: <#{VOICE_ID}> 2 · <#{AFK_ID}> 1"
    for text in (report, status_report(view), health_report(view)):
        for member_id in (MEMBER_ID, OTHER_MEMBER_ID, THIRD_MEMBER_ID):
            assert str(member_id) not in text


async def test_voice_report_when_idle(app):
    report = voice_report(await snapshot(app.services, app.settings))

    assert report.splitlines()[:2] == ["Voice: not in voice", "People in voice: nobody"]


async def test_voice_report_while_he_waits_to_leave(app):
    s = app.services
    await s.session.ensure_connected(PREDECESSOR_ID)
    s.session.schedule_disconnect()  # nobody is left: he leaves after the grace period

    report = voice_report(await snapshot(s, app.settings))

    assert report.splitlines()[0] == f"Voice: in <#{PREDECESSOR_ID}>, leaving shortly"


async def hold_from_before_the_change(app: App) -> None:
    """A hold stored when a drag still held Amir out (reason "moved"), read back at startup:
    it holds until an admin lifts it."""
    await VoiceHoldRepo(app.db).save(VoiceHold(since=app.clock.now(), reason="moved"))
    await app.services.companion.load_hold()


async def test_reports_show_a_hold(app):
    app.members.append(Member(MEMBER_ID))
    await hold_from_before_the_change(app)

    view = await snapshot(app.services, app.settings)

    assert view.voice_hold == VoiceHold(since=app.clock.now(), reason="moved")
    held = (
        f"Voice: held out of voice since <t:{unix(app.clock.now())}:R> (moved);"
        " use /amir voice reconnect"
    )
    assert voice_report(view).splitlines()[0] == held
    assert f"{held} · 0 greeting clips ready" in status_report(view).splitlines()
    assert f"{held} · 0 greeting clips ready" in health_report(view).splitlines()
    assert f"People in voice: <#{VOICE_ID}> 1" in voice_report(view).splitlines()


async def test_a_cleared_hold_leaves_the_reports(app):
    await hold_from_before_the_change(app)
    await app.services.companion.release_hold()

    view = await snapshot(app.services, app.settings)

    assert view.voice_hold is None
    assert voice_report(view).splitlines()[0] == "Voice: not in voice"


async def test_cycles_report_shows_each_world(app, captured):
    assert "No cycle data yet" in cycles_report(await snapshot(app.services, app.settings))

    await app.poll_world()
    report = cycles_report(await snapshot(app.services, app.settings))

    for snap in rolled_capture(captured, app):
        assert WORLD_NAMES[snap.world] in report
        assert f"<t:{unix(snap.expiry)}:R>" in report


async def test_failing_cycle_cards_show_in_health_and_cycles(make_app, monkeypatch):
    app = await make_app(publishing=True)
    vallis = WORLD_NAMES["vallis"]
    real_send = app.sender.send

    async def send(channel, *, cards=(), text=None, role_ping_id=None):
        if any(card.title == vallis for card in cards):
            raise PermissionDenied("missing Send Messages")
        return await real_send(channel, cards=cards, text=text, role_ping_id=role_ping_id)

    monkeypatch.setattr(app.sender, "send", send)
    view = await snapshot(app.services, app.settings)
    assert "Cycle cards: no failures" in health_report(view)

    await app.poll_world()  # feeds the cycles and ticks: the Orb Vallis card cannot be posted
    view = await snapshot(app.services, app.settings)

    retry = app.clock.now() + timedelta(seconds=RETRY_BASE_SECONDS)
    assert view.cycles_failing == (("vallis", "PermissionDenied", retry),)
    health = health_report(view)
    assert f"Cycle cards: failing for {vallis} (`PermissionDenied`," in health
    assert f"next try <t:{unix(retry)}:R>" in health
    cycles = cycles_report(view).splitlines()
    at = next(i for i, line in enumerate(cycles) if line.startswith(f"• {vallis}:"))
    assert cycles[at + 1] == (
        f"  Card not updating: `PermissionDenied`; next try <t:{unix(retry)}:R>"
    )
    assert sum("not updating" in line for line in cycles) == 1


async def test_baro_report_without_a_visit(app):
    report = baro_report(await snapshot(app.services, app.settings))

    assert "No Baro Ki'Teer visit is known yet." in report


async def test_baro_report_lists_upcoming_milestones(app, captured):
    visit = parse_void_trader(captured(OFFICIAL_CAPTURE))
    await app.poll_world()

    report = baro_report(await snapshot(app.services, app.settings))

    assert f"Last call: <t:{unix(last_call(captured))}:f>" in report
    assert f"<t:{unix(visit.activation)}:" not in report  # arrival has passed
    assert "Next Baro milestone: Last call" in status_report(
        await snapshot(app.services, app.settings)
    )

    app.clock.advance((visit.expiry - app.clock.now()).total_seconds() + 1)
    report = baro_report(await snapshot(app.services, app.settings))
    assert "last known visit is over" in report


async def test_health_report_summarises_every_part(app, captured):
    report = health_report(await snapshot(app.services, app.settings))
    assert "Background tasks: none running" in report
    assert "World state: not polled yet" in report

    await app.poll_world()
    await app.services.news.poll_all()
    forum = next(s for s in app.settings.news.sources if s.kind == "forum_rss")
    app.fetcher.queued[forum.url] = [captured(STEAM_CAPTURE)]
    await app.services.news.poll_all()
    report = health_report(await snapshot(app.services, app.settings))

    assert "Publishing: OFF (previews only)" in report
    assert "World state: official ok, WFCD ok, 0 errors in the last poll" in report
    assert f"News sources: 1 of 2 failing (`{forum.id}`)" in report
    assert "Outbox: 0 failed, 1 pending" in report


async def test_config_report_is_the_check_config_report(app):
    report = config_report(app.settings)

    assert report.splitlines() == [
        f"Amir Beckett {__version__}: the configuration is valid",
        "Publishing: OFF (previews only)",
        "Missing runtime IDs: none",
        "News sources: " + ", ".join(f"{s.id} (on)" for s in app.settings.news.sources),
    ]
