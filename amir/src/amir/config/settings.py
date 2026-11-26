"""Validated settings: TOML file for configuration, environment for the token."""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

_STRICT = ConfigDict(extra="forbid", frozen=True)

_RUNTIME_ID_FIELDS = (
    "guild_id",
    "voice_channel_id",
    "news_channel_id",
    "cycles_channel_id",
    "voice_log_channel_id",
)


class ConfigError(Exception):
    """Invalid configuration. Problems hold messages and dotted locations only, never values."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class _Model(BaseModel):
    model_config = _STRICT


class DiscordSettings(_Model):
    guild_id: int = Field(0, ge=0)
    voice_channel_id: int = Field(0, ge=0)
    news_channel_id: int = Field(0, ge=0)
    cycles_channel_id: int = Field(0, ge=0)
    voice_log_channel_id: int = Field(0, ge=0)
    notification_role_id: int = Field(0, ge=0)


class PublishingSettings(_Model):
    enabled: bool = False


class VoiceSettings(_Model):
    greetings_dir: str = "assets/greetings"
    ffmpeg_path: str = "ffmpeg"
    audio_filter: str = "loudnorm=I=-16:TP=-1.5:LRA=11"
    leave_grace_seconds: int = Field(60, ge=0)
    greeting_expiry_seconds: int = Field(30, ge=1)
    queue_capacity: int = Field(5, ge=1)
    rejoin_window_seconds: int = Field(120, ge=0)
    greet_on_rejoin: bool = False
    announce_joins: bool = True
    announce_leaves: bool = True
    announce_moves: bool = True
    connect_timeout_seconds: int = Field(15, ge=1)
    reconnect_max_attempts: int = Field(5, ge=1)
    exclude_channel_ids: list[int] = []
    follow_settle_seconds: int = Field(30, ge=0)


class NewsSourceSettings(_Model):
    id: str = Field(min_length=1)
    kind: Literal["forum_rss", "steam_news"]
    label: str = Field(min_length=1)
    url: str = Field(min_length=1)
    interval_seconds: int = Field(ge=1)
    enabled: bool = True


class NewsSettings(_Model):
    exclude_title_keywords: list[str] = [
        "baro ki'teer",
        "soundtrack",
        "single release",
        "tennogen",
        "battlekit",
        "bundle",
        "merch",
        "plush",
    ]
    role_ping_categories: list[str] = []
    sources: list[NewsSourceSettings] = []

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, value: list[NewsSourceSettings]) -> list[NewsSourceSettings]:
        """Dedup and baseline state are keyed by source id, so two sources may not share one."""
        seen: set[str] = set()
        repeats: list[str] = []
        for index, source in enumerate(value):
            if source.id in seen:
                repeats.append(str(index))
            seen.add(source.id)
        if repeats:
            raise ValueError(f"ids must be unique; repeated at index {', '.join(repeats)}")
        return value


class WorldStateSettings(_Model):
    official_url: str = "https://api.warframe.com/cdn/worldState.php"
    wfcd_url: str = "https://api.warframestat.us/pc"
    items_url: str = "https://api.warframestat.us/items/?only=uniqueName,name"
    interval_seconds: int = Field(300, ge=1)
    flip_refresh_delay_seconds: int = Field(5, ge=0)
    items_refresh_hours: int = Field(24, ge=1)


class CyclesSettings(_Model):
    worlds: list[str] = ["cetus", "vallis", "cambion"]
    durations: dict[str, int] = {
        "day": 6000,
        "night": 3000,
        "warm": 400,
        "cold": 1200,
        "fass": 6000,
        "vome": 3000,
    }

    @field_validator("durations")
    @classmethod
    def _positive(cls, value: dict[str, int]) -> dict[str, int]:
        bad = sorted(k for k, v in value.items() if v < 1)
        if bad:
            raise ValueError(f"durations must be positive seconds: {', '.join(bad)}")
        return value


class BaroSettings(_Model):
    warn_before_arrival_hours: int = Field(72, ge=0)
    warn_before_departure_hours: int = Field(10, ge=0)


class HttpSettings(_Model):
    connect_timeout_seconds: int = Field(10, ge=1)
    read_timeout_seconds: int = Field(30, ge=1)
    max_retries: int = Field(3, ge=0)
    max_response_bytes: int = Field(5_000_000, ge=1)
    user_agent: str = "AmirBeckett/0.1 (+Discord bot)"


class StorageSettings(_Model):
    db_path: str = "data/amir.sqlite3"


class LoggingSettings(_Model):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    heartbeat_path: str = "data/heartbeat"
    voice_debug: bool = False


class Settings(_Model):
    discord: DiscordSettings = DiscordSettings()
    publishing: PublishingSettings = PublishingSettings()
    voice: VoiceSettings = VoiceSettings()
    news: NewsSettings = NewsSettings()
    worldstate: WorldStateSettings = WorldStateSettings()
    cycles: CyclesSettings = CyclesSettings()
    baro: BaroSettings = BaroSettings()
    http: HttpSettings = HttpSettings()
    storage: StorageSettings = StorageSettings()
    logging: LoggingSettings = LoggingSettings()
    token: SecretStr

    def missing_runtime_ids(self) -> list[str]:
        """Dotted names of required Discord IDs that are still 0."""
        return [f"discord.{n}" for n in _RUNTIME_ID_FIELDS if getattr(self.discord, n) == 0]


def default_config_path(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("AMIR_CONFIG", "config/amir.toml"))


def _dotted(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in loc) or "<root>"


def load_settings(path: Path, environ: Mapping[str, str]) -> Settings:
    problems: list[str] = []
    name = Path(path).name  # problems name the file, never where it lives
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError([f"config file not found: {name}"]) from None
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__  # strerror never includes the path
        raise ConfigError([f"config file {name} cannot be read: {reason}"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"config file {name} is not valid TOML: {exc}"]) from None
    except UnicodeDecodeError as exc:
        raise ConfigError([f"config file {name} is not valid UTF-8 (byte {exc.start})"]) from None

    if "token" in data:
        problems.append("token: must not be set in the config file; use DISCORD_TOKEN")
        data = {k: v for k, v in data.items() if k != "token"}

    token = environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        problems.append("DISCORD_TOKEN: environment variable is required")

    try:
        settings = Settings.model_validate({**data, "token": token or "unset"})
    except ValidationError as exc:
        problems.extend(f"{_dotted(e['loc'])}: {e['msg']}" for e in exc.errors())
    if problems:
        raise ConfigError(problems)
    return settings
