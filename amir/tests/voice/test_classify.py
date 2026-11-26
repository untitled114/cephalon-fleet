import pytest

from amir.voice.classify import VoiceEventKind, classify

WARFRAME, PREDECESSOR = 1001, 1002
OWN = 2001
MEMBER = 3001


@pytest.mark.parametrize(
    ("before", "after", "kind"),
    [
        (None, PREDECESSOR, VoiceEventKind.JOIN),
        (None, WARFRAME, VoiceEventKind.JOIN),
        (PREDECESSOR, None, VoiceEventKind.LEAVE),
        (WARFRAME, PREDECESSOR, VoiceEventKind.MOVE),
        (PREDECESSOR, WARFRAME, VoiceEventKind.MOVE),
        (WARFRAME, WARFRAME, None),  # mute, deafen, stream: no channel change
        (PREDECESSOR, PREDECESSOR, None),
        (None, None, None),
    ],
)
def test_every_channel_change_is_classified(before, after, kind):
    result = classify(
        member_id=MEMBER,
        is_bot=False,
        own_id=OWN,
        before_channel_id=before,
        after_channel_id=after,
    )
    assert result is kind


@pytest.mark.parametrize(
    ("is_bot", "member_id"),
    [(True, MEMBER), (False, OWN)],
    ids=["bot", "amir_himself"],
)
@pytest.mark.parametrize(
    ("before", "after"),
    [(None, WARFRAME), (WARFRAME, None), (WARFRAME, PREDECESSOR)],
)
def test_bots_and_amir_are_never_classified(is_bot, member_id, before, after):
    result = classify(
        member_id=member_id,
        is_bot=is_bot,
        own_id=OWN,
        before_channel_id=before,
        after_channel_id=after,
    )
    assert result is None


def test_classify_is_keyword_only():
    with pytest.raises(TypeError):
        classify(MEMBER, False, OWN, None, WARFRAME)  # type: ignore[misc]
