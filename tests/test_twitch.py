from unittest.mock import MagicMock

import pytest
import yt_dlp

from vsummary.util import twitch


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
        twitch._download("https://www.twitch.tv/videos/123456789", str(tmp_path), 120)
