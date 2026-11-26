"""A resolved prediction whose result Lunara changed on a re-check: Lumen sends one correction DM.

Real messages only: the one correction Lunara sent after its tracker fix (Sion James, OVER 3.5
rebounds, 3 → 4 at 11:31:39 ET on 2026-11-10, on the scoreboard and again on CLE @ CHA's
socket) and that slate's /picks/today from the night before.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

import ws_listener
from formatter import PickFormatter
from tests.test_tracking_only import ADVICE_TALK
from tests.test_ws_listener import FakeConnectCtx, _stop_after
from ws_listener import WSListener

LUMEN = Path(__file__).parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "corrections" / "2026-11-10"
CLE_AT_CHA = "401909953"


def _feed() -> dict[str, str]:
    """Each socket's correction message as Lumen receives it, keyed by socket."""
    rows = [json.loads(line) for line in (FIXTURE / "feed.jsonl").read_text().splitlines()]
    return {row["source"]: row["raw"] for row in rows}


def _picks_today() -> list[dict]:
    return json.loads(json.loads((FIXTURE / "picks_today.jsonl").read_text())["raw"])


@pytest.fixture
def listener():
    alerts = yaml.safe_load((LUMEN / "config.yaml").read_text())["alerts"]
    return WSListener(
        api_url="http://127.0.0.1:8010",
        ws_url="ws://127.0.0.1:8010/ws",
        alert_cfg=alerts,
        send_dm=AsyncMock(),
        formatter=PickFormatter(),
    )


async def _receive(listener, monkeypatch, socket: str, raw: str) -> None:
    """One message on one socket, through the listener's own read loop."""
    monkeypatch.setattr(ws_listener.websockets, "connect", lambda url: FakeConnectCtx([raw]))
    monkeypatch.setattr(ws_listener.asyncio, "sleep", _stop_after(listener))
    if socket == "scoreboard":
        await listener._listen_scoreboard()
    else:
        await listener._listen_game(socket)
    listener._running = True


def _dms(listener) -> list:
    sent = []
    while not listener._dm_queue.empty():
        sent.append(listener._dm_queue.get_nowait())
    return sent


async def test_the_scoreboard_correction_sends_one_dm(listener, monkeypatch):
    await _receive(listener, monkeypatch, "scoreboard", _feed()["scoreboard"])

    [(content, embed)] = _dms(listener)
    assert content is None
    assert embed.title == "CORRECTED: Sion James"
    pick = json.loads(_feed()["scoreboard"])["data"]["picks"][0]
    context = " | ".join(x for x in (pick["tier"], pick["model_version"].upper()) if x)
    assert embed.description == (
        "**OVER 3.5 REBOUNDS**\n"
        "Final: 3.0 → **4.0**\n"
        "Result: MISS → **HIT**\n"
        "Lunara re-checked the final box score.\n" + context
    )
    assert embed.color.value == 0x22C55E


async def test_both_copies_of_a_correction_send_one_dm(listener, monkeypatch):
    # The game's socket brought the same correction 2 ms after the scoreboard.
    feed = _feed()
    await _receive(listener, monkeypatch, "scoreboard", feed["scoreboard"])
    await _receive(listener, monkeypatch, CLE_AT_CHA, feed[CLE_AT_CHA])

    [(_, embed)] = _dms(listener)
    assert embed.title == "CORRECTED: Sion James"


async def test_the_game_socket_copy_is_a_correction_not_a_result(listener, monkeypatch):
    await _receive(listener, monkeypatch, CLE_AT_CHA, _feed()[CLE_AT_CHA])

    [(_, embed)] = _dms(listener)
    assert embed.title == "CORRECTED: Sion James"


async def test_a_pick_still_on_the_slate_takes_its_corrected_result(listener, monkeypatch):
    # Before the slate rolls over, the recap still to come counts the pick as corrected.
    listener.engine.register_picks(_picks_today())
    await _receive(listener, monkeypatch, "scoreboard", _feed()["scoreboard"])

    pick = listener.engine.games[CLE_AT_CHA].picks[5]
    assert (pick.actual_value, pick.is_hit) == (4.0, True)
    assert pick in listener.engine.get_all_resolved_today()


async def test_corrections_follow_the_results_setting(listener, monkeypatch):
    listener.alert_cfg = {**listener.alert_cfg, "hit_miss": False}
    await _receive(listener, monkeypatch, "scoreboard", _feed()["scoreboard"])

    assert _dms(listener) == []


async def test_a_correction_speaks_of_the_pick(listener, monkeypatch):
    await _receive(listener, monkeypatch, "scoreboard", _feed()["scoreboard"])

    [(_, embed)] = _dms(listener)
    text = json.dumps(embed.to_dict(), ensure_ascii=False).lower()
    assert not [word for word in ADVICE_TALK if word in text]
