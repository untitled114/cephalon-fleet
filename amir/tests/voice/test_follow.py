import dataclasses

import pytest

from amir.voice.follow import ChannelView, Choice, VoiceCensus, choose

WF, DBD, MOCHANDO, PRED, TRADING, AFK = 11, 12, 13, 14, 15, 16
ORDER = (WF, DBD, MOCHANDO, PRED, TRADING, AFK)
NOT_CANDIDATES = {TRADING, AFK}


def census(**people: int) -> VoiceCensus:
    """`people` maps a channel name to how many humans sit in it; server order is ORDER."""
    counts = {
        WF: people.get("WF", 0),
        DBD: people.get("DBD", 0),
        MOCHANDO: people.get("MOCHANDO", 0),
        PRED: people.get("PRED", 0),
        TRADING: people.get("TRADING", 0),
        AFK: people.get("AFK", 0),
    }
    views = tuple(
        ChannelView(
            channel_id=channel_id,
            humans=frozenset(channel_id * 100 + n for n in range(counts[channel_id])),
            candidate=channel_id not in NOT_CANDIDATES,
            can_speak=True,
        )
        for channel_id in ORDER
    )
    return VoiceCensus(channels=views, afk_channel_id=AFK)


@pytest.mark.parametrize(
    ("people", "current", "pin", "expected"),
    [
        pytest.param(
            {"WF": 2, "PRED": 3}, WF, None, Choice(PRED, True, False), id="biggest-after-settle"
        ),
        pytest.param({"WF": 2, "PRED": 2}, PRED, None, Choice(PRED, False, False), id="tie-stays"),
        pytest.param(
            {"DBD": 1, "WF": 1}, None, None, Choice(WF, False, False), id="fresh-tie-picks-home"
        ),
        pytest.param(
            {"MOCHANDO": 1, "DBD": 1},
            None,
            None,
            Choice(DBD, False, False),
            id="fresh-tie-server-order",
        ),
        pytest.param(
            {"WF": 0, "PRED": 2}, WF, None, Choice(PRED, False, False), id="empty-moves-at-once"
        ),
        pytest.param(
            {"AFK": 4, "TRADING": 3, "WF": 1},
            None,
            None,
            Choice(WF, False, False),
            id="afk-and-capped-never",
        ),
        pytest.param({"AFK": 2}, WF, None, Choice(None, False, False), id="nobody-anywhere"),
        pytest.param(
            {"TRADING": 1, "PRED": 3}, TRADING, TRADING, Choice(TRADING, False, True), id="pin"
        ),
        pytest.param(
            {"TRADING": 0, "PRED": 3}, TRADING, TRADING, Choice(PRED, False, False), id="empty-pin"
        ),
        pytest.param({"AFK": 2, "WF": 1}, AFK, None, Choice(WF, False, False), id="out-of-afk"),
    ],
)
def test_choose(people, current, pin, expected):
    assert choose(census(**people), current=current, pin=pin, home=WF) == expected


def test_the_biggest_group_wins_after_the_settle_time():
    got = choose(census(WF=2, PRED=3), current=WF, pin=None, home=WF)
    assert got == Choice(PRED, settle=True, pinned=False)


def test_a_current_non_candidate_with_people_is_left_at_once_after_a_settle():
    # Amir sits in a capped channel (dragged there, say) with people; a candidate has more.
    got = choose(census(TRADING=1, PRED=3), current=TRADING, pin=None, home=WF)
    assert got == Choice(PRED, settle=True, pinned=False)


def test_census_queries():
    c = census(WF=2, PRED=1, TRADING=3, AFK=4)
    assert c.count(WF) == 2
    assert c.count(999) == 0
    assert c.humans_in(PRED) == frozenset({PRED * 100})
    assert c.humans_in(999) == frozenset()
    assert c.candidates_with_people() == 3  # TRADING and AFK are not candidates


def test_can_speak_in_reads_his_role_per_channel():
    c = census(WF=1)
    silent = VoiceCensus(
        tuple(dataclasses.replace(v, can_speak=v.channel_id != PRED) for v in c.channels),
        afk_channel_id=AFK,
    )
    assert silent.can_speak_in(WF) is True
    assert silent.can_speak_in(PRED) is False
    assert silent.can_speak_in(999) is False  # no such channel: nowhere to speak
    assert VoiceCensus.EMPTY.can_speak_in(WF) is False


def test_the_empty_census_has_nobody():
    assert VoiceCensus.EMPTY.channels == ()
    assert VoiceCensus.EMPTY.afk_channel_id is None
    assert VoiceCensus.EMPTY.candidates_with_people() == 0
    assert choose(VoiceCensus.EMPTY, current=None, pin=None, home=WF) == Choice(None, False, False)
