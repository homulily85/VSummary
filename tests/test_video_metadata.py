import importlib
from unittest.mock import MagicMock

import pytest


def test_extract_video_metadata_returns_title_and_channel(monkeypatch):
    metadata = importlib.import_module("vsummary.util.video_metadata")
    downloader = MagicMock()
    downloader.__enter__.return_value = downloader
    downloader.extract_info.return_value = {
        "title": "Test Stream",
        "channel": "Test Channel",
    }
    monkeypatch.setattr(metadata.yt_dlp, "YoutubeDL", lambda _: downloader)

    assert metadata._extract_video_metadata("https://example.test/video") == (
        metadata.VideoMetadata(title="Test Stream", channel_name="Test Channel")
    )


def test_extract_video_metadata_wraps_unexpected_lookup_errors(monkeypatch):
    metadata = importlib.import_module("vsummary.util.video_metadata")
    downloader = MagicMock()
    downloader.__enter__.side_effect = OSError("network failed")
    monkeypatch.setattr(metadata.yt_dlp, "YoutubeDL", lambda _: downloader)

    with pytest.raises(metadata.VideoMetadataError, match="network failed"):
        metadata._extract_video_metadata("https://example.test/video")
