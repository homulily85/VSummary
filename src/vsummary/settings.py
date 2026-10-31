"""Validated TOML operational configuration and environment-backed secrets."""

from __future__ import annotations

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv

DATABASE_NAME = "VSummary"


class SettingsError(ValueError):
    """Runtime secrets or operational configuration are missing or invalid."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeConfig:
    """Non-secret operational settings with one shared source of defaults."""

    auto_summary_channel_id: int | None = None
    discord_send_interval_seconds: float = 1.0
    poll_interval_minutes: float = 30.0
    manual_poll_interval_seconds: float = 10.0
    job_lease_seconds: float = 900.0
    source_retry_limit: int = 5
    delivery_retry_limit: int = 5
    retry_initial_delay_seconds: float = 3600.0
    retry_multiplier: float = 2.0
    invalid_summary_retries: int = 1
    transcript_delay_seconds: float = 7200.0
    output_language: str = "English"
    youtube_yt_dlp_fallback_enabled: bool = False
    max_audio_mib: float = 190.0
    audio_bitrate_kbps: int = 64
    audio_download_format: str = "bestaudio/best"
    source_upload_timeout_seconds: float = 600.0
    log_file_path: Path = Path("logs/vsummary.log")
    log_level: str = "INFO"
    log_rotation_interval_days: int = 1
    log_backup_count: int = 14
    discord_log_channel_id: int | None = None
    discord_log_queue_size: int = 1000
    discord_log_min_level: str = "WARNING"

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> RuntimeConfig:
        """Validate sectioned TOML values, rejecting unknown keys and wrong types."""
        if not isinstance(values, Mapping):
            raise SettingsError("configuration must contain TOML sections")
        parsed = {}
        for section, options in values.items():
            if section not in _CONFIG_SECTIONS:
                raise SettingsError(f"Unknown configuration section: {section}")
            if not isinstance(options, Mapping):
                raise SettingsError(f"{section} must be a TOML table")
            for key, value in options.items():
                name = _CONFIG_SECTIONS[section].get(key)
                if name is None:
                    raise SettingsError(
                        f"Unknown configuration option: {section}.{key}"
                    )
                parsed[name] = _config_value(name, value, f"{section}.{key}")
        return cls(**parsed)

    def retry_delay_seconds(self, failure_count: int) -> float:
        """Return the existing exponential job backoff with configured parameters."""
        return self.retry_initial_delay_seconds * self.retry_multiplier ** (
            failure_count - 1
        )


DEFAULT_CONFIG = RuntimeConfig()

_CONFIG_SECTIONS = {
    "discord": {
        "auto_summary_channel_id": "auto_summary_channel_id",
        "send_interval_seconds": "discord_send_interval_seconds",
    },
    "workers": {
        "poll_interval_minutes": "poll_interval_minutes",
        "manual_poll_interval_seconds": "manual_poll_interval_seconds",
        "lease_seconds": "job_lease_seconds",
    },
    "retries": {
        "source_limit": "source_retry_limit",
        "delivery_limit": "delivery_retry_limit",
        "initial_delay_seconds": "retry_initial_delay_seconds",
        "multiplier": "retry_multiplier",
        "invalid_response_retries": "invalid_summary_retries",
    },
    "autosummary": {"transcript_delay_seconds": "transcript_delay_seconds"},
    "summary": {"output_language": "output_language"},
    "youtube": {"yt_dlp_fallback_enabled": "youtube_yt_dlp_fallback_enabled"},
    "media": {
        "max_audio_mib": "max_audio_mib",
        "bitrate_kbps": "audio_bitrate_kbps",
        "download_format": "audio_download_format",
        "upload_timeout_seconds": "source_upload_timeout_seconds",
    },
    "logging": {
        "path": "log_file_path",
        "level": "log_level",
        "rotation_interval_days": "log_rotation_interval_days",
        "backup_count": "log_backup_count",
        "discord_channel_id": "discord_log_channel_id",
        "discord_queue_size": "discord_log_queue_size",
        "discord_min_level": "discord_log_min_level",
    },
}


@dataclass(frozen=True, slots=True)
class Settings(RuntimeConfig):
    """Application composition of validated secrets and operational settings."""

    discord_token: str = dataclass_field(repr=False)
    mongodb_uri: str = dataclass_field(repr=False)
    mongodb_database: ClassVar[str] = DATABASE_NAME

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str | None],
        *,
        config: Mapping[str, object] | None = None,
    ) -> Settings:
        """Read only supported secrets from the environment and options from TOML."""
        operational = RuntimeConfig.from_mapping({} if config is None else config)
        return cls(
            discord_token=_required(values, "DISCORD_TOKEN"),
            mongodb_uri=_required(values, "MONGODB_URI"),
            **asdict(operational),
        )


def load_settings(
    values: Mapping[str, str | None] | None = None,
    *,
    dotenv_path: str | Path | None = None,
    load_dotenv_file: bool = True,
    config_path: str | Path | None = None,
) -> Settings:
    """Load secrets and optional working-directory TOML once at startup."""
    if load_dotenv_file:
        load_dotenv(dotenv_path)
    path = Path("config.toml") if config_path is None else Path(config_path)
    try:
        with path.open("rb") as source:
            config = tomllib.load(source)
    except FileNotFoundError as exc:
        if config_path is not None:
            raise SettingsError(f"Configuration file not found: {path}") from exc
        config = {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"Could not load configuration {path}: {exc}") from exc
    return Settings.from_mapping(
        os.environ if values is None else values, config=config
    )


def runtime_config(settings: object | None = None) -> RuntimeConfig:
    """Use injected settings or central defaults, including lightweight test adapters."""
    if settings is None:
        return DEFAULT_CONFIG
    if type(settings) is RuntimeConfig:
        return settings
    return RuntimeConfig(
        **{
            field.name: getattr(
                settings, field.name, getattr(DEFAULT_CONFIG, field.name)
            )
            for field in fields(RuntimeConfig)
        }
    )


def configured_auto_summary_channel_id(
    settings: RuntimeConfig | None = None,
) -> int | None:
    """Return the configured destination without consulting process environment."""
    return runtime_config(settings).auto_summary_channel_id


def _required(values: Mapping[str, str | None], name: str) -> str:
    value = values.get(name)
    if not value or not value.strip():
        raise SettingsError(f"{name} is required")
    return value.strip()


def _config_value(name: str, value: object, label: str) -> object:
    default = getattr(DEFAULT_CONFIG, name)
    if default is None:
        if type(value) is not int or value <= 0:
            raise SettingsError(
                f"{label} must be a positive integer; omit it to disable"
            )
        return value
    if isinstance(default, bool):
        if type(value) is not bool:
            raise SettingsError(f"{label} must be a TOML boolean")
        return value
    if isinstance(default, (int, float)):
        if type(value) not in {int, float} or (
            type(default) is int and type(value) is not int
        ):
            raise SettingsError(f"{label} has an invalid numeric type")
        nonnegative = name in {
            "source_retry_limit",
            "delivery_retry_limit",
            "invalid_summary_retries",
            "log_backup_count",
            "transcript_delay_seconds",
        }
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or value < 0 or (not nonnegative and value == 0):
            raise SettingsError(
                f"{label} must be finite and {'nonnegative' if nonnegative else 'positive'}"
            )
        if name == "retry_multiplier" and value < 1:
            raise SettingsError(f"{label} must be at least 1")
        return value
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise SettingsError(f"{label} must be a non-blank string")
    if name in {"log_level", "discord_log_min_level"}:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise SettingsError(
                f"{label} must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )
        return level
    return Path(value) if isinstance(default, Path) else value.strip()
