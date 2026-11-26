"""Tests for the URL override in the `settings` module.

`settings.resolve_lunara_urls(config, environ) -> tuple[str, str]` lets environment
values win over `config["lunara"]`.
"""

CFG = {
    "lunara": {
        "api_url": "https://lunara.example.invalid",
        "ws_url": "wss://lunara.example.invalid/ws",
    }
}


def test_env_overrides_config():
    from settings import resolve_lunara_urls

    env = {"LUNARA_API_URL": "http://127.0.0.1:8010", "LUNARA_WS_URL": "ws://127.0.0.1:8010/ws"}
    assert resolve_lunara_urls(CFG, env) == ("http://127.0.0.1:8010", "ws://127.0.0.1:8010/ws")


def test_config_used_when_env_absent():
    from settings import resolve_lunara_urls

    assert resolve_lunara_urls(CFG, {}) == (
        "https://lunara.example.invalid",
        "wss://lunara.example.invalid/ws",
    )


def test_blank_env_does_not_override():
    from settings import resolve_lunara_urls

    assert resolve_lunara_urls(CFG, {"LUNARA_API_URL": "  "})[0] == "https://lunara.example.invalid"


def test_the_owner_id_comes_from_the_environment():
    from settings import resolve_owner_id

    assert resolve_owner_id({"LUMEN_OWNER_ID": " 111 "}) == 111


def test_a_missing_or_malformed_owner_id_is_none():
    from settings import resolve_owner_id

    for env in ({}, {"LUMEN_OWNER_ID": ""}, {"LUMEN_OWNER_ID": "the-owner"}):
        assert resolve_owner_id(env) is None


def test_the_tracked_config_names_no_discord_id():
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "config.yaml").read_text()
    assert re.findall(r"\b\d{17,19}\b", text) == []
