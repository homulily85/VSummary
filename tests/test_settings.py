import json
import os
import tomllib
from pathlib import Path

import pytest

from vsummary import settings as settings_module
from vsummary.settings import Settings, SettingsError, load_settings

SECRETS = {
    "DISCORD_TOKEN": "token",
    "MONGODB_URI": "mongodb://localhost",
    "HOLODEX_API_KEY": "test-holodex-key",
}


def test_env_example_contains_only_supported_application_secrets():
    names = {
        line.partition("=")[0]
        for line in Path(".env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert names == set(SECRETS)


def test_config_example_matches_builtin_defaults():
    parsed = tomllib.loads(Path("config.example.toml").read_text())
    assert (
        settings_module.RuntimeConfig.from_mapping(parsed)
        == settings_module.DEFAULT_CONFIG
    )
    assert "database" not in parsed
    assert "holodex" not in parsed
    assert parsed["summary"]["output_language"] == "English"


def test_output_language_defaults_to_english():
    assert settings_module.RuntimeConfig().output_language == "English"
    assert Settings.from_mapping(SECRETS).output_language == "English"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (" Japanese ", "Japanese"),
        ("日本語", "日本語"),
    ],
)
def test_output_language_loads_from_toml(tmp_path, language, expected):
    path = tmp_path / "config.toml"
    path.write_text(
        "[summary]\noutput_language = " + json.dumps(language), encoding="utf-8"
    )
    settings = load_settings(SECRETS, config_path=path, load_dotenv_file=False)
    assert settings.output_language == expected
    assert settings_module.runtime_config(settings).output_language == expected


def test_settings_loads_typed_toml_options_and_keeps_database_fixed():
    settings = Settings.from_mapping(
        SECRETS,
        config={
            "discord": {"auto_summary_channel_id": 123, "send_interval_seconds": 0.5},
            "workers": {
                "poll_interval_minutes": 15,
                "manual_poll_interval_seconds": 3,
                "lease_seconds": 120,
            },
            "retries": {
                "source_limit": 4,
                "delivery_limit": 3,
                "initial_delay_seconds": 7,
                "multiplier": 3,
                "invalid_response_retries": 0,
            },
            "autosummary": {"transcript_delay_seconds": 60},
            "youtube": {"yt_dlp_fallback_enabled": True},
            "media": {
                "max_audio_mib": 20,
                "bitrate_kbps": 32,
                "download_format": "worstaudio",
                "upload_timeout_seconds": 9,
            },
            "logging": {
                "path": "var/log/bot.log",
                "level": "debug",
                "rotation_interval_days": 2,
                "backup_count": 3,
                "discord_channel_id": 456,
                "discord_queue_size": 4,
                "discord_min_level": "error",
            },
        },
    )
    assert settings.discord_token == "token"
    assert settings.mongodb_database == "VSummary"
    assert settings.auto_summary_channel_id == 123
    assert settings.discord_send_interval_seconds == 0.5
    assert settings.poll_interval_minutes == 15
    assert settings.manual_poll_interval_seconds == 3
    assert settings.job_lease_seconds == 120
    assert settings.source_retry_limit == 4
    assert settings.delivery_retry_limit == 3
    assert settings.retry_initial_delay_seconds == 7
    assert settings.retry_multiplier == 3
    assert settings.invalid_summary_retries == 0
    assert settings.transcript_delay_seconds == 60
    assert settings.youtube_yt_dlp_fallback_enabled is True
    assert settings.max_audio_mib == 20
    assert settings.audio_bitrate_kbps == 32
    assert settings.audio_download_format == "worstaudio"
    assert settings.source_upload_timeout_seconds == 9
    assert settings.log_file_path == Path("var/log/bot.log")
    assert settings.log_level == "DEBUG"
    assert settings.log_rotation_interval_days == 2
    assert settings.log_backup_count == 3
    assert settings.discord_log_channel_id == 456
    assert settings.discord_log_queue_size == 4
    assert settings.discord_log_min_level == "ERROR"


def test_nonsecret_environment_values_are_ignored():
    values = SECRETS | {
        "AUTO_SUMMARY_CHANNEL_ID": "123",
        "DISCORD_SEND_INTERVAL_SECONDS": "0.1",
        "POLL_INTERVAL_MINUTES": "2",
        "SOURCE_RETRY_LIMIT": "2",
        "DELIVERY_RETRY_LIMIT": "2",
        "MONGODB_DATABASE": "other",
        "HTTP_TIMEOUT_SECONDS": "0.1",
        "LOG_FILE_PATH": "other.log",
        "LOG_LEVEL": "DEBUG",
        "DISCORD_LOG_CHANNEL_ID": "456",
        "YOUTUBE_YT_DLP_FALLBACK_ENABLED": "true",
        "TWITCH_MAX_DURATION_SECONDS": "1",
        "OUTPUT_LANGUAGE": "Japanese",
        "SUMMARY_OUTPUT_LANGUAGE": "Vietnamese",
    }
    assert Settings.from_mapping(values) == Settings.from_mapping(SECRETS)
    assert settings_module.configured_auto_summary_channel_id() is None


def test_default_config_is_optional_but_explicit_missing_path_fails(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert load_settings(SECRETS, load_dotenv_file=False) == Settings.from_mapping(
        SECRETS
    )
    with pytest.raises(SettingsError, match="missing.toml"):
        load_settings(SECRETS, config_path="missing.toml", load_dotenv_file=False)


def test_default_and_explicit_config_paths_are_loaded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config.toml").write_text("[youtube]\nyt_dlp_fallback_enabled = true\n")
    assert (
        load_settings(SECRETS, load_dotenv_file=False).youtube_yt_dlp_fallback_enabled
        is True
    )
    Path("other.toml").write_text("[discord]\nsend_interval_seconds = 2.5\n")
    assert (
        load_settings(
            SECRETS, config_path="other.toml", load_dotenv_file=False
        ).discord_send_interval_seconds
        == 2.5
    )


def test_dotenv_secrets_preserve_environment_precedence_and_notebooklm_auth(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "environment-token")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.setenv("HOLODEX_API_KEY", "  environment-holodex-key  ")
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "DISCORD_TOKEN=file-token\nMONGODB_URI=mongodb://file\n"
        "HOLODEX_API_KEY=file-holodex-key\nNOTEBOOKLM_PROFILE=bot\nLOG_LEVEL=DEBUG\n"
    )
    settings = load_settings(dotenv_path=dotenv)
    assert settings.discord_token == "environment-token"
    assert settings.mongodb_uri == "mongodb://file"
    assert settings.holodex_api_key == "environment-holodex-key"
    assert settings.log_level == "INFO"
    assert os.environ["NOTEBOOKLM_PROFILE"] == "bot"
    monkeypatch.delenv("HOLODEX_API_KEY")
    assert load_settings(dotenv_path=dotenv).holodex_api_key == "file-holodex-key"
    os.environ.pop("MONGODB_URI")
    os.environ.pop("NOTEBOOKLM_PROFILE")
    os.environ.pop("LOG_LEVEL", None)


@pytest.mark.parametrize(
    "text",
    [
        "[broken",
        "[database]\nname='other'",
        "[discord]\nunknown=1",
        "discord=1",
    ],
)
def test_invalid_or_unsupported_config_is_rejected(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(SettingsError):
        load_settings(SECRETS, config_path=path, load_dotenv_file=False)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("youtube", "yt_dlp_fallback_enabled", "true"),
        ("workers", "lease_seconds", 0),
        ("workers", "manual_poll_interval_seconds", float("nan")),
        pytest.param("workers", "lease_seconds", 10**400, id="oversized-number"),
        ("workers", "poll_interval_minutes", float("inf")),
        ("discord", "auto_summary_channel_id", True),
        ("discord", "send_interval_seconds", -1),
        ("retries", "invalid_response_retries", -1),
        ("retries", "source_limit", True),
        ("retries", "multiplier", 0.5),
        ("autosummary", "transcript_delay_seconds", -1),
        ("media", "bitrate_kbps", 1.5),
        ("media", "max_audio_mib", 0),
        ("media", "download_format", ""),
        ("logging", "level", "verbose"),
        ("logging", "discord_min_level", "bad"),
        ("logging", "discord_queue_size", 0),
        ("logging", "path", ""),
        ("summary", "output_language", " \t "),
        ("summary", "output_language", "Japanese\x00"),
        ("summary", "output_language", False),
        ("summary", "output_language", None),
    ],
)
def test_invalid_typed_config_values_are_rejected(section, key, value):
    with pytest.raises(SettingsError, match=key):
        Settings.from_mapping(SECRETS, config={section: {key: value}})


def test_zero_retry_counts_and_optional_channels():
    settings = Settings.from_mapping(
        SECRETS,
        config={
            "retries": {
                "source_limit": 0,
                "delivery_limit": 0,
                "invalid_response_retries": 0,
            },
            "autosummary": {"transcript_delay_seconds": 0},
        },
    )
    assert (
        settings.source_retry_limit
        == settings.delivery_retry_limit
        == settings.invalid_summary_retries
        == 0
    )
    assert settings.auto_summary_channel_id is settings.discord_log_channel_id is None


def test_missing_required_secrets_still_fail():
    with pytest.raises(SettingsError, match="DISCORD_TOKEN"):
        Settings.from_mapping({})
    with pytest.raises(SettingsError, match="MONGODB_URI"):
        Settings.from_mapping({"DISCORD_TOKEN": "token"})


@pytest.mark.parametrize("key", [None, "", " \t "])
def test_holodex_key_is_required(key):
    values = SECRETS | {"HOLODEX_API_KEY": key}
    if key is None:
        del values["HOLODEX_API_KEY"]
    with pytest.raises(SettingsError, match="HOLODEX_API_KEY is required"):
        Settings.from_mapping(values)


def test_operational_injection_does_not_forward_or_represent_secrets():
    values = {
        "DISCORD_TOKEN": "discord-secret-value",
        "MONGODB_URI": "mongodb://user:secret-value@localhost",
        "HOLODEX_API_KEY": "holodex-secret-value",
    }
    settings = Settings.from_mapping(values, config={"media": {"bitrate_kbps": 32}})
    config = settings_module.runtime_config(settings)
    assert type(config) is settings_module.RuntimeConfig
    assert not hasattr(config, "discord_token")
    assert not hasattr(config, "mongodb_uri")
    assert not hasattr(config, "holodex_api_key")
    assert config.audio_bitrate_kbps == 32
    assert all(value not in repr(settings) for value in values.values())
