from amir.news.filtering import exclusion_reason


def test_returns_keyword_as_given() -> None:
    assert exclusion_reason("Baro Ki'Teer Returns", ["BARO ki'teer"]) == "BARO ki'teer"


def test_first_matching_keyword_wins() -> None:
    assert exclusion_reason("Soundtrack Bundle", ["bundle", "soundtrack"]) == "bundle"


def test_no_match_is_none() -> None:
    assert exclusion_reason("Devstream #198", ["bundle"]) is None
    assert exclusion_reason("anything", []) is None


def test_casefold_handles_unicode() -> None:
    assert exclusion_reason("GROSSE Straße", ["strasse"]) == "strasse"


def test_empty_keyword_ignored() -> None:
    assert exclusion_reason("anything", [""]) is None
