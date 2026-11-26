import json
from pathlib import Path

import pytest

from amir.config import ConfigError, default_config_path, load_settings
from amir.config.settings import Settings, VoiceSettings

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "amir.example.toml"
README = EXAMPLE.parents[1] / "README.md"
ENV = {"DISCORD_TOKEN": "t"}


def write_variant(tmp_path: Path, old: str, new: str) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "amir.toml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


def test_example_config_loads():
    settings = load_settings(EXAMPLE, ENV)
    assert settings.publishing.enabled is False
    assert settings.voice.queue_capacity == 5
    assert [s.id for s in settings.news.sources] == ["forum-pc-updates", "steam-announcements"]
    assert settings.cycles.durations["warm"] == 400
    assert settings.token.get_secret_value() == "t"


def test_greetings_play_about_as_loud_as_the_owners_clip():
    # -20 LUFS sounded a little low. -16 LUFS plays the clip (measured
    # -16.4 LUFS, true peak +0.07 dBTP) about 4 dB louder, close to its own level, with only
    # its loudest syllable held at -1.5 dBTP; it stays one even gain over the whole clip.
    assert VoiceSettings().audio_filter == "loudnorm=I=-16:TP=-1.5:LRA=11"


def test_publishing_defaults_off_when_section_missing(tmp_path):
    path = write_variant(tmp_path, "[publishing]\nenabled = false", "")
    assert load_settings(path, ENV).publishing.enabled is False


@pytest.mark.parametrize(
    "env", [{}, {"DISCORD_TOKEN": ""}, {"DISCORD_TOKEN": "   "}, {"DISCORD_TOKEN": " \t\n"}]
)
def test_missing_token_names_variable(env):
    with pytest.raises(ConfigError) as exc:
        load_settings(EXAMPLE, env)
    assert "DISCORD_TOKEN" in " ".join(exc.value.problems)


def test_whitespace_token_is_not_echoed():
    with pytest.raises(ConfigError) as exc:
        load_settings(EXAMPLE, {"DISCORD_TOKEN": "   "})
    assert "'   '" not in " ".join(exc.value.problems)
    assert "'unset'" not in " ".join(exc.value.problems)


def test_token_is_stripped():
    settings = load_settings(EXAMPLE, {"DISCORD_TOKEN": "  abc.def.ghi \n"})
    assert settings.token.get_secret_value() == "abc.def.ghi"


def test_invalid_value_reports_dotted_path(tmp_path):
    path = write_variant(tmp_path, "queue_capacity = 5", "queue_capacity = 0")
    with pytest.raises(ConfigError) as exc:
        load_settings(path, ENV)
    assert any("voice.queue_capacity" in p for p in exc.value.problems)


def test_unknown_key_rejected(tmp_path):
    path = write_variant(tmp_path, "[voice]\n", "[voice]\ntypo = 1\n")
    with pytest.raises(ConfigError) as exc:
        load_settings(path, ENV)
    assert any("voice.typo" in p for p in exc.value.problems)


def test_nested_source_error_path_and_no_values_leak(tmp_path):
    path = write_variant(tmp_path, 'kind = "forum_rss"', 'kind = "hunter2-secret"')
    with pytest.raises(ConfigError) as exc:
        load_settings(path, ENV)
    joined = " ".join(exc.value.problems)
    assert "news.sources.0.kind" in joined
    assert "hunter2-secret" not in joined


def test_token_in_toml_rejected_without_leaking(tmp_path):
    path = tmp_path / "amir.toml"
    path.write_text('token = "abc.def.ghi"\n' + EXAMPLE.read_text(encoding="utf-8"), "utf-8")
    with pytest.raises(ConfigError) as exc:
        load_settings(path, ENV)
    problems = exc.value.problems
    assert any(p.startswith("token:") for p in problems)
    assert "abc.def.ghi" not in " ".join(problems)


def test_token_never_in_repr():
    settings = load_settings(EXAMPLE, {"DISCORD_TOKEN": "abc.def.ghi"})
    assert "abc.def.ghi" not in repr(settings)
    assert "abc.def.ghi" not in str(settings)


def test_missing_runtime_ids():
    assert load_settings(EXAMPLE, ENV).missing_runtime_ids() == [
        "discord.guild_id",
        "discord.voice_channel_id",
        "discord.news_channel_id",
        "discord.cycles_channel_id",
        "discord.voice_log_channel_id",
    ]


def test_missing_runtime_ids_shrinks_when_set(tmp_path):
    path = write_variant(tmp_path, "guild_id = 0", "guild_id = 42")
    assert "discord.guild_id" not in load_settings(path, ENV).missing_runtime_ids()


def test_settings_are_frozen():
    settings = load_settings(EXAMPLE, ENV)
    with pytest.raises(ValueError):
        settings.publishing.enabled = True


def test_missing_file_and_bad_toml(tmp_path):
    with pytest.raises(ConfigError) as missing:
        load_settings(tmp_path / "nope.toml", ENV)
    bad = tmp_path / "bad.toml"
    bad.write_text("[voice\n", encoding="utf-8")
    with pytest.raises(ConfigError) as invalid:
        load_settings(bad, ENV)
    unreadable = tmp_path / "dir.toml"
    unreadable.mkdir()  # a directory where the file should be
    with pytest.raises(ConfigError) as not_a_file:
        load_settings(unreadable, ENV)

    for error, name in ((missing, "nope.toml"), (invalid, "bad.toml"), (not_a_file, "dir.toml")):
        text = " ".join(error.value.problems)
        assert name in text  # the file's name, so the operator knows which file
        assert str(tmp_path) not in text  # never where it lives


def test_default_config_path():
    assert default_config_path({}) == Path("config/amir.toml")
    assert default_config_path({"AMIR_CONFIG": "x/y.toml"}) == Path("x/y.toml")


def test_non_positive_cycle_duration_rejected(tmp_path):
    path = write_variant(tmp_path, "warm = 400", "warm = 0")
    with pytest.raises(ConfigError) as exc:
        load_settings(path, ENV)
    assert any("cycles.durations" in p and "warm" in p for p in exc.value.problems)


def test_duplicate_source_id_rejected(tmp_path):
    path = write_variant(tmp_path, 'id = "steam-announcements"', 'id = "forum-pc-updates"')
    with pytest.raises(ConfigError) as exc:
        load_settings(path, ENV)
    (problem,) = exc.value.problems
    assert problem.startswith("news.sources:")
    assert "unique" in problem and "1" in problem  # the entry that repeats an earlier id
    assert "forum-pc-updates" not in problem  # locations only, never values


def test_config_that_is_not_utf8_is_a_config_error(tmp_path):
    raw = EXAMPLE.read_bytes()
    marker = "· PC Update Notes".encode()  # the example's one non-ASCII character, in UTF-8
    assert marker in raw
    path = tmp_path / "latin1.toml"
    path.write_bytes(raw.replace(marker, "· PC Update Notes".encode("latin-1"), 1))
    with pytest.raises(ConfigError) as exc:
        load_settings(path, ENV)
    (problem,) = exc.value.problems
    assert "latin1.toml" in problem and "UTF-8" in problem
    assert str(tmp_path) not in problem


def toml_literal(value: object) -> str:
    """A default as the README writes it: TOML, with inline tables spaced as in the example."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_literal(v) for v in value) + "]"
    assert isinstance(value, dict)
    return "{ " + ", ".join(f"{k} = {toml_literal(v)}" for k, v in value.items()) + " }"


def test_readme_shows_every_default():
    rows = {
        line.split("|")[1].strip(): line
        for line in README.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `[")
    }
    # The five Discord IDs are required (the example leaves them 0, unset), and sources have no
    # default entries: the README says so instead of showing a default.
    required = {*load_settings(EXAMPLE, ENV).missing_runtime_ids(), "news.sources"}
    assert len(required) == 6
    for section, field in Settings.model_fields.items():
        if section == "token":
            continue
        row = rows[f"`[{section}]`"]
        for name, value in field.default:
            if f"{section}.{name}" not in required:
                assert f"`{name} = {toml_literal(value)}`" in row, f"{section}.{name}"
