import pytest

from vsummary.settings import Settings, SettingsError, load_settings


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
        }
    )

    assert settings.discord_token == "token"
    assert settings.mongodb_database == "summaries"
    assert settings.auto_summary_channel_id == 123
    assert settings.poll_interval_minutes == 15
    assert settings.http_timeout_seconds == 8.5


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
