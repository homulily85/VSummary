from pathlib import Path

import pytest

from vsummary.settings import Settings, SettingsError, load_settings


def test_env_example_lists_every_runtime_configuration_variable():
    example = Path(".env.example").read_text(encoding="utf-8")
    names = {
        line.partition("=")[0]
        for line in example.splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    assert names == {
        "DISCORD_TOKEN",
        "MONGODB_URI",
        "MONGODB_DATABASE",
        "AUTO_SUMMARY_CHANNEL_ID",
        "DISCORD_LOG_CHANNEL_ID",
        "DISCORD_SEND_INTERVAL_SECONDS",
        "POLL_INTERVAL_MINUTES",
        "SOURCE_RETRY_LIMIT",
        "DELIVERY_RETRY_LIMIT",
        "HTTP_TIMEOUT_SECONDS",
        "TWITCH_MAX_DURATION_SECONDS",
        "LOG_FILE_PATH",
        "LOG_LEVEL",
    }


def test_settings_loads_and_validates_runtime_configuration():
    settings = Settings.from_mapping(
        {
            "DISCORD_TOKEN": "token",
            "MONGODB_URI": "mongodb://localhost:27017",
            "MONGODB_DATABASE": "summaries",
            "AUTO_SUMMARY_CHANNEL_ID": "123",
            "POLL_INTERVAL_MINUTES": "15",
            "SOURCE_RETRY_LIMIT": "4",
            "DELIVERY_RETRY_LIMIT": "3",
            "HTTP_TIMEOUT_SECONDS": "8.5",
            "LOG_FILE_PATH": "var/log/vsummary.log",
            "LOG_LEVEL": "debug",
            "DISCORD_LOG_CHANNEL_ID": "456",
            "DISCORD_SEND_INTERVAL_SECONDS": "0.5",
        }
    )

    assert settings.discord_token == "token"
    assert settings.mongodb_database == "summaries"
    assert settings.auto_summary_channel_id == 123
    assert settings.poll_interval_minutes == 15
    assert settings.http_timeout_seconds == 8.5
    assert settings.log_file_path == Path("var/log/vsummary.log")
    assert settings.log_level == "DEBUG"
    assert settings.discord_log_channel_id == 456
    assert settings.discord_send_interval_seconds == 0.5


def test_settings_uses_logging_defaults_and_rejects_invalid_logging_values():
    defaults = Settings.from_mapping(
        {"DISCORD_TOKEN": "token", "MONGODB_URI": "mongodb://localhost"}
    )

    assert defaults.log_file_path == Path("logs/vsummary.log")
    assert defaults.log_level == "INFO"
    assert defaults.discord_log_channel_id is None
    assert defaults.discord_send_interval_seconds == 1.0

    for name, value in (
        ("LOG_LEVEL", "verbose"),
        ("DISCORD_LOG_CHANNEL_ID", "not-an-integer"),
        ("DISCORD_LOG_CHANNEL_ID", "0"),
        ("DISCORD_SEND_INTERVAL_SECONDS", "0"),
    ):
        values = {
            "DISCORD_TOKEN": "token",
            "MONGODB_URI": "mongodb://localhost",
            name: value,
        }
        with pytest.raises(SettingsError):
            Settings.from_mapping(values)


def test_settings_rejects_missing_and_invalid_values():
    with pytest.raises(SettingsError):
        Settings.from_mapping({"DISCORD_TOKEN": "token"})

    with pytest.raises(SettingsError):
        Settings.from_mapping(
            {
                "DISCORD_TOKEN": "token",
                "MONGODB_URI": "mongodb://localhost",
                "AUTO_SUMMARY_CHANNEL_ID": "not-an-integer",
            }
        )


def test_load_settings_can_read_a_mapping_without_import_side_effects():
    settings = load_settings(
        {
            "DISCORD_TOKEN": "token",
            "MONGODB_URI": "mongodb://localhost",
            "AUTO_SUMMARY_CHANNEL_ID": "9",
        },
        load_dotenv_file=False,
    )

    assert settings == Settings.from_mapping(
        {
            "DISCORD_TOKEN": "token",
            "MONGODB_URI": "mongodb://localhost",
            "AUTO_SUMMARY_CHANNEL_ID": "9",
        }
    )
