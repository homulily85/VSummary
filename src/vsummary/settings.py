from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class SettingsError(ValueError):
    """Raised when runtime configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    mongodb_uri: str
    mongodb_database: str = "VSummary"
    auto_summary_channel_id: int | None = None
    poll_interval_minutes: int = 30
    source_retry_limit: int = 5
    delivery_retry_limit: int = 5
    http_timeout_seconds: float = 15.0
    log_file_path: Path = Path("logs/vsummary.log")
    log_level: str = "INFO"
    discord_log_channel_id: int | None = None
    twitch_max_duration_seconds: int = 21_600
    discord_send_interval_seconds: float = 1.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, str | None]) -> Settings:
        token = _required(values, "DISCORD_TOKEN")
        mongodb_uri = _required(values, "MONGODB_URI")
        auto_channel = _optional_int(values.get("AUTO_SUMMARY_CHANNEL_ID"))
        poll_interval = _positive_int(values.get("POLL_INTERVAL_MINUTES"), 30)
        source_retries = _positive_int(values.get("SOURCE_RETRY_LIMIT"), 5)
        delivery_retries = _positive_int(values.get("DELIVERY_RETRY_LIMIT"), 5)
        timeout = _positive_float(values.get("HTTP_TIMEOUT_SECONDS"), 15.0)
        log_file_path = _log_file_path(values.get("LOG_FILE_PATH"))
        log_level = _log_level(values.get("LOG_LEVEL"))
        discord_log_channel = _optional_int(
            values.get("DISCORD_LOG_CHANNEL_ID"), "DISCORD_LOG_CHANNEL_ID"
        )
        twitch_max_duration = _positive_int(
            values.get("TWITCH_MAX_DURATION_SECONDS"), 21_600
        )
        discord_send_interval = _positive_float(
            values.get("DISCORD_SEND_INTERVAL_SECONDS"), 1.0
        )
        return cls(
            discord_token=token,
            mongodb_uri=mongodb_uri,
            mongodb_database=values.get("MONGODB_DATABASE") or "VSummary",
            auto_summary_channel_id=auto_channel,
            poll_interval_minutes=poll_interval,
            source_retry_limit=source_retries,
            delivery_retry_limit=delivery_retries,
            http_timeout_seconds=timeout,
            log_file_path=log_file_path,
            log_level=log_level,
            discord_log_channel_id=discord_log_channel,
            twitch_max_duration_seconds=twitch_max_duration,
            discord_send_interval_seconds=discord_send_interval,
        )


def load_settings(
    values: Mapping[str, str | None] | None = None,
    *,
    dotenv_path: str | Path | None = None,
    load_dotenv_file: bool = True,
) -> Settings:
    """Load and validate environment configuration at application startup."""
    if load_dotenv_file:
        load_dotenv(dotenv_path)
    return Settings.from_mapping(os.environ if values is None else values)


def configured_auto_summary_channel_id(settings: Settings | None = None) -> int | None:
    """Return the validated auto-summary channel, including legacy env fallback."""
    if settings is not None and settings.auto_summary_channel_id is not None:
        return settings.auto_summary_channel_id
    return _optional_int(os.environ.get("AUTO_SUMMARY_CHANNEL_ID"))


def _required(values: Mapping[str, str | None], name: str) -> str:
    value = values.get(name)
    if not value or not value.strip():
        raise SettingsError(f"{name} is required")
    return value.strip()


def _optional_int(
    value: str | None, name: str = "AUTO_SUMMARY_CHANNEL_ID"
) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise SettingsError(f"{name} must be positive")
    return parsed


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError) as exc:
        raise SettingsError("configuration value must be an integer") from exc
    if parsed <= 0:
        raise SettingsError("configuration value must be positive")
    return parsed


def _positive_float(value: str | None, default: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError) as exc:
        raise SettingsError("HTTP_TIMEOUT_SECONDS must be a number") from exc
    if parsed <= 0:
        raise SettingsError("HTTP_TIMEOUT_SECONDS must be positive")
    return parsed


def _log_file_path(value: str | None) -> Path:
    if value is None or not value.strip():
        return Path("logs/vsummary.log")
    try:
        return Path(value.strip())
    except (TypeError, ValueError) as exc:
        raise SettingsError("LOG_FILE_PATH must be a valid path") from exc


def _log_level(value: str | None) -> str:
    level = (value or "INFO").strip().upper()
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if level not in valid_levels:
        raise SettingsError(
            "LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, or CRITICAL"
        )
    return level
