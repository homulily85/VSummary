from unittest.mock import MagicMock

import pytest
import yt_dlp

from vsummary.util import generic_media


def _downloader(monkeypatch, info):
    monkeypatch.setattr(generic_media, "_ensure_public_host", lambda _: None)
    downloader = MagicMock()
    downloader.__enter__.return_value = downloader
    downloader.extract_info.return_value = info
    monkeypatch.setattr(generic_media.yt_dlp, "YoutubeDL", lambda _: downloader)
    return downloader


def test_inspect_accepts_single_public_media(monkeypatch):
    downloader = _downloader(
        monkeypatch,
        {"id": "123", "title": "Talk", "uploader": "Speaker", "_type": "video"},
    )

    metadata = generic_media.inspect_media("https://vimeo.com/123")

    assert metadata.title == "Talk"
    assert metadata.channel_name == "Speaker"
    downloader.extract_info.assert_called_once_with(
        "https://vimeo.com/123", download=False
    )


@pytest.mark.parametrize(
    "info",
    [
        {"_type": "playlist", "entries": [{"id": "one"}]},
        {"_type": "multi_video", "entries": [{"id": "one"}]},
        {"id": "live", "live_status": "is_live"},
        {"id": "soon", "live_status": "is_upcoming"},
    ],
)
def test_inspect_rejects_collections_and_live_media(monkeypatch, info):
    _downloader(monkeypatch, info)
    with pytest.raises(generic_media.GenericMediaUnavailableError):
        generic_media.inspect_media("https://example.com/media")


def test_inspect_wraps_unexpected_extractor_failure(monkeypatch):
    downloader = _downloader(monkeypatch, {"id": "one"})
    downloader.extract_info.side_effect = ValueError("extractor changed")

    with pytest.raises(generic_media.GenericMediaError, match="inspect"):
        generic_media.inspect_media("https://example.com/media")


def test_download_converts_one_media_and_checks_result(monkeypatch, tmp_path):
    monkeypatch.setattr(generic_media.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    downloader = _downloader(monkeypatch, {"id": "123", "title": "Talk"})
    output = tmp_path / "audio.m4a"
    output.write_bytes(b"audio")

    assert generic_media._download("https://vimeo.com/123", str(tmp_path)) == output
    downloader.process_info.assert_called_once_with({"id": "123", "title": "Talk"})
    downloader.download.assert_not_called()


def test_download_classifies_transient_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(generic_media.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    downloader = _downloader(monkeypatch, {"id": "123"})
    downloader.process_info.side_effect = yt_dlp.utils.DownloadError("HTTP 503")

    with pytest.raises(generic_media.GenericMediaError):
        generic_media._download("https://vimeo.com/123", str(tmp_path))


def test_dns_resolving_to_private_network_is_rejected(monkeypatch):
    monkeypatch.setattr(
        generic_media.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("10.0.0.2", 443))],
    )

    with pytest.raises(generic_media.GenericMediaUnavailableError, match="Private"):
        generic_media._ensure_public_host("https://media.example.org/item")
