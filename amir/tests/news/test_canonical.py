import pytest

from amir.news.canonical import canonicalize_url


def test_strips_tracking_keeps_destination() -> None:
    url = (
        "https://www.warframe.com/news/warframes-halloween-2026-events"
        "?utm_medium=in-game&utm_source=in-game&utm_campaign=2026-10-NaberusLaunch"
    )
    assert canonicalize_url(url) == "https://www.warframe.com/news/warframes-halloween-2026-events"
    assert canonicalize_url("https://example.com/p?id=5&utm_x=1") == "https://example.com/p?id=5"
    assert (
        canonicalize_url("https://www.warframe.com/en/news/x") == "https://www.warframe.com/news/x"
    )


def test_normalises_scheme_host_port_fragment_and_keeps_query_order() -> None:
    assert (
        canonicalize_url(
            "HTTPS://Example.COM:443/a?z=1&fbclid=x&a=2&gclid=y&mc_cid=1&mc_eid=2#frag"
        )
        == "https://example.com/a?z=1&a=2"
    )
    assert canonicalize_url("http://example.com:80/a") == "http://example.com/a"
    assert canonicalize_url("http://example.com:8080/a") == "http://example.com:8080/a"


def test_en_news_rewrite_only_on_warframe_host() -> None:
    assert canonicalize_url("https://example.com/en/news/x") == "https://example.com/en/news/x"


@pytest.mark.parametrize("url", ["http://host:abc/x", "http://host:99999/x", "http://[::1/x"])
def test_malformed_urls_returned_unchanged(url: str) -> None:
    assert canonicalize_url(f"  {url} ") == url


@pytest.mark.parametrize(
    "url",
    [
        "https://e.com/p?flag&a=1",
        "https://e.com/p?q=a%20b",
        "https://e.com/p?r=1,2",
        "https://e.com/p?s=%FF",
    ],
)
def test_query_preserved_verbatim(url: str) -> None:
    assert canonicalize_url(url) == url


def test_tracking_keys_case_insensitive_and_verbatim_survivors() -> None:
    assert canonicalize_url("https://e.com/p?UTM_source=a&flag&q=a%20b&FBclid=1") == (
        "https://e.com/p?flag&q=a%20b"
    )


def test_userinfo_preserved_verbatim_while_host_is_lowercased() -> None:
    assert canonicalize_url("https://u:p@Example.com/x") == "https://u:p@example.com/x"
    assert canonicalize_url("https://User@Example.com:443/x") == "https://User@example.com/x"
    assert canonicalize_url("http://u:p%40q@[::1]:8080/x") == "http://u:p%40q@[::1]:8080/x"
