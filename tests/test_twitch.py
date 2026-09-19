from unittest.mock import MagicMock

import pytest
import yt_dlp

from vsummary.util import twitch


def test_download_allows_long_vods_and_logs_audio_preparation(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setattr(twitch.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    downloader = MagicMock()
    downloader.__enter__.return_value = downloader
    downloader.extract_info.return_value = {"duration": 86_400}
    monkeypatch.setattr(twitch.yt_dlp, "YoutubeDL", lambda _: downloader)
    output = tmp_path / "123456789.m4a"
    output.write_bytes(b"audio")

    with caplog.at_level("INFO", logger=twitch.__name__):
        result = twitch._download(
            "https://www.twitch.tv/videos/123456789", str(tmp_path)
        )

    assert result == output
    downloader.download.assert_called_once_with(
        ["https://www.twitch.tv/videos/123456789"]
    )
    assert "Downloading Twitch VOD audio for 123456789." in caplog.messages
    assert "Prepared Twitch VOD audio for 123456789." in caplog.messages


def test_download_preserves_yt_dlp_postprocessing_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(twitch.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    downloader = MagicMock()
    downloader.__enter__.return_value = downloader
    downloader.extract_info.return_value = {"duration": 60}
    downloader.download.side_effect = yt_dlp.utils.DownloadError(
        "ERROR: Postprocessing: Conversion failed!"
    )
    monkeypatch.setattr(twitch.yt_dlp, "YoutubeDL", lambda _: downloader)

    with pytest.raises(
        twitch.TwitchAudioUnavailableError,
        match="Postprocessing: Conversion failed!",
    ):
        twitch._download("https://www.twitch.tv/videos/123456789", str(tmp_path))


def test_download_rejects_audio_that_exceeds_notebooklm_upload_limit(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(twitch.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    downloader = MagicMock()
    downloader.__enter__.return_value = downloader
    downloader.extract_info.return_value = {"id": "123456789", "duration": 86_400}
    monkeypatch.setattr(twitch.yt_dlp, "YoutubeDL", lambda _: downloader)
    (tmp_path / "123456789.m4a").write_bytes(b"audio")
    monkeypatch.setattr(twitch, "MAX_AUDIO_BYTES", 0)

    with pytest.raises(twitch.TwitchAudioUnavailableError, match="upload limit"):
        twitch._download("https://www.twitch.tv/videos/123456789", str(tmp_path))
