"""Lumen's morning summary goes out once per slate, even when Lumen restarts.

Every Lunara release restarts Lumen. On 2026-11-10 the summary went out at 06:02 and again after
the 11:30 and 12:30 restarts (Lumen's own log): the mark that it had gone out lived
only in memory. The picks are a real slate's /picks/today (2026-11-09, 22:02:58 ET).
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import time_machine
import yaml

import ws_listener
from formatter import PickFormatter
from ws_listener import WSListener

LUMEN = Path(__file__).parent.parent
PICKS = Path(__file__).parent / "fixtures" / "corrections" / "2026-11-10" / "picks_today.jsonl"
EASTERN = ZoneInfo("America/New_York")
POLLED = datetime(2026, 11, 9, 22, 2, 58, tzinfo=EASTERN)


def _lumen(monkeypatch) -> WSListener:
    """Lumen as it starts, polling the real slate; its sockets and player lookups left out."""
    listener = WSListener(
        api_url="http://127.0.0.1:8010",
        ws_url="ws://127.0.0.1:8010/ws",
        alert_cfg=yaml.safe_load((LUMEN / "config.yaml").read_text())["alerts"],
        send_dm=AsyncMock(),
        formatter=PickFormatter(),
    )
    picks = json.loads(json.loads(PICKS.read_text())["raw"])
    monkeypatch.setattr(listener, "_fetch_picks", AsyncMock(return_value=picks))
    monkeypatch.setattr(listener, "_listen_game", AsyncMock())
    monkeypatch.setattr(listener, "_fetch_season_stats_for_picks", AsyncMock())
    return listener


def _summaries(listener) -> list[str]:
    sent = []
    while not listener._dm_queue.empty():
        _, embed = listener._dm_queue.get_nowait()
        sent.append(embed.title)
    return [title for title in sent if "Today's Picks" in title]


async def test_a_restart_does_not_send_the_slates_summary_again(monkeypatch):
    with time_machine.travel(POLLED, tick=False):
        before = _lumen(monkeypatch)
        await before._poll_and_subscribe()
        after = _lumen(monkeypatch)  # a Lunara release restarts Lumen
        await after._poll_and_subscribe()

    assert len(_summaries(before)) == 1
    assert _summaries(after) == []


async def test_the_next_slate_gets_its_own_summary(monkeypatch):
    with time_machine.travel(POLLED, tick=False):
        await _lumen(monkeypatch)._poll_and_subscribe()
    with time_machine.travel(datetime(2026, 11, 10, 6, 2, 4, tzinfo=EASTERN), tick=False):
        lumen = _lumen(monkeypatch)
        lumen._roll_slate_day()

    assert lumen._summary_sent is False


async def test_the_summary_goes_out_when_its_mark_cannot_be_kept(monkeypatch, tmp_path):
    (tmp_path / "not-a-folder").write_text("")
    monkeypatch.setattr(ws_listener, "SUMMARY_SENT_FILE", str(tmp_path / "not-a-folder" / "mark"))

    with time_machine.travel(POLLED, tick=False):
        lumen = _lumen(monkeypatch)
        await lumen._poll_and_subscribe()

    assert len(_summaries(lumen)) == 1
    assert set(lumen._game_tasks) == {"401909956", "401909957"}  # the games still pending


async def test_a_restart_still_looks_up_the_players_season_averages(monkeypatch):
    # They were looked up only with the summary, so the restart that skipped it went without.
    with time_machine.travel(POLLED, tick=False):
        await _lumen(monkeypatch)._poll_and_subscribe()
        after = _lumen(monkeypatch)
        await after._poll_and_subscribe()

    after._fetch_season_stats_for_picks.assert_called_once_with(after._all_picks)
