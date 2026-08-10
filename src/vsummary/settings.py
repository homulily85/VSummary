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

    @classmethod
    def from_mapping(cls, values: Mapping[str, str | None]) -> Settings:
        token = _required(values, "DISCORD_TOKEN")
        mongodb_uri = _required(values, "MONGODB_URI")
        auto_channel = _optional_int(values.get("AUTO_SUMMARY_CHANNEL_ID"))
        poll_interval = _positive_int(values.get("POLL_INTERVAL_MINUTES"), 30)
        source_retries = _positive_int(values.get("SOURCE_RETRY_LIMIT"), 5)
        delivery_retries = _positive_int(values.get("DELIVERY_RETRY_LIMIT"), 5)
        timeout = _positive_float(values.get("HTTP_TIMEOUT_SECONDS"), 15.0)
        return cls(
            discord_token=token,
            mongodb_uri=mongodb_uri,
            mongodb_database=values.get("MONGODB_DATABASE") or "VSummary",
            auto_summary_channel_id=auto_channel,
            poll_interval_minutes=poll_interval,
            source_retry_limit=source_retries,
            delivery_retry_limit=delivery_retries,
            http_timeout_seconds=timeout,
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


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError("AUTO_SUMMARY_CHANNEL_ID must be an integer") from exc
    if parsed <= 0:
        raise SettingsError("AUTO_SUMMARY_CHANNEL_ID must be positive")
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
