from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yt_dlp

from vsummary.util.video import VideoRef

SPACE_ID = "1OwxWwQOPlNxQ"


@pytest.mark.parametrize(
    "value",
    [
        f"https://x.com/i/spaces/{SPACE_ID}",
        f"https://twitter.com/i/spaces/{SPACE_ID}?s=20",
        f"https://mobile.twitter.com/i/spaces/{SPACE_ID}?ref=mobile",
        f"https://m.x.com/i/spaces/{SPACE_ID}",
    ],
)
def test_parse_x_space_input_accepts_direct_urls_with_mobile_hosts_and_queries(value):
    from vsummary.util.x_space import XSpaceInputKind, parse_x_space_input

    parsed = parse_x_space_input(value)

    assert parsed is not None
    assert parsed.kind is XSpaceInputKind.SPACE
    assert parsed.input_id == SPACE_ID


@pytest.mark.parametrize(
    "value",
    [
        "https://x.com/example/status/1234567890123456789",
        "https://twitter.com/example/status/1234567890123456789?s=20",
        "https://mobile.twitter.com/example/status/1234567890123456789?ref=mobile",
        "https://m.x.com/example/status/1234567890123456789",
    ],
)
def test_parse_x_space_input_accepts_status_urls_with_mobile_hosts_and_queries(value):
    from vsummary.util.x_space import XSpaceInputKind, parse_x_space_input

    parsed = parse_x_space_input(value)

    assert parsed is not None
    assert parsed.kind is XSpaceInputKind.STATUS
    assert parsed.input_id == "1234567890123456789"


@pytest.mark.asyncio
async def test_resolve_x_space_normalizes_a_status_url_to_its_archived_space(
    monkeypatch, caplog
):
    from vsummary.util import x_space

    monkeypatch.setattr(
        x_space,
        "_extract_x_space_info",
        lambda _: {
            "extractor_key": "TwitterSpaces",
            "id": SPACE_ID,
            "live_status": "was_live",
            "formats": [{"url": "https://media.example/space.m3u8"}],
        },
    )

    with caplog.at_level("INFO", logger=x_space.__name__):
        ref = await x_space.resolve_x_space(
            "https://twitter.com/example/status/1234567890123456789?s=20"
        )

    assert ref == VideoRef(source="x_space", video_id=SPACE_ID)
    assert "Resolved archived X Space 1OwxWwQOPlNxQ." in caplog.messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_status", "message"),
    [
        ("is_upcoming", "has not started yet"),
        ("is_live", "currently live"),
        ("post_live", "ended but has not been archived yet"),
    ],
)
async def test_resolve_x_space_rejects_non_archived_states(
    monkeypatch, live_status, message
):
    from vsummary.util import x_space

    monkeypatch.setattr(
        x_space,
        "_extract_x_space_info",
        lambda _: {
            "extractor_key": "TwitterSpaces",
            "id": SPACE_ID,
            "live_status": live_status,
            "formats": [{"url": "https://media.example/space.m3u8"}],
        },
    )

    with pytest.raises(x_space.XSpaceResolutionError, match=message):
        await x_space.resolve_x_space(f"https://x.com/i/spaces/{SPACE_ID}")


@pytest.mark.asyncio
async def test_resolve_x_space_rejects_an_ended_space_without_a_replay(monkeypatch):
    from vsummary.util import x_space

    monkeypatch.setattr(
        x_space,
        "_extract_x_space_info",
        lambda _: {
            "extractor_key": "TwitterSpaces",
            "id": SPACE_ID,
            "live_status": "was_live",
            "formats": [],
        },
    )

    with pytest.raises(
        x_space.XSpaceResolutionError, match="ended but is not archived"
    ):
        await x_space.resolve_x_space(f"https://x.com/i/spaces/{SPACE_ID}")


@pytest.mark.asyncio
async def test_resolve_x_space_rejects_a_status_post_without_a_space(monkeypatch):
    from vsummary.util import x_space

    monkeypatch.setattr(
        x_space,
        "_extract_x_space_info",
        lambda _: {
            "extractor_key": "Twitter",
            "id": "1234567890123456789",
            "formats": [{"url": "https://media.example/video.mp4"}],
        },
    )

    with pytest.raises(
        x_space.XSpaceResolutionError, match="post does not contain an X Space"
    ):
        await x_space.resolve_x_space(
            "https://x.com/example/status/1234567890123456789"
        )


@pytest.mark.asyncio
async def test_resolve_x_space_translates_yt_dlp_replay_disabled_errors(monkeypatch):
    from vsummary.util import x_space

    monkeypatch.setattr(
        x_space,
        "_extract_x_space_info",
        lambda _: (_ for _ in ()).throw(
            yt_dlp.utils.DownloadError("Twitter Space ended and replay is disabled")
        ),
    )

    with pytest.raises(
        x_space.XSpaceResolutionError, match="ended but is not archived"
    ):
        await x_space.resolve_x_space(f"https://x.com/i/spaces/{SPACE_ID}")


def test_download_x_space_audio_has_no_duration_limit_but_rejects_oversized_audio(
    monkeypatch, tmp_path
):
    from vsummary.util import x_space

    monkeypatch.setattr(x_space.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    downloader = MagicMock()
    downloader.__enter__.return_value = downloader
    downloader.extract_info.return_value = {
        "extractor_key": "TwitterSpaces",
        "id": SPACE_ID,
        "live_status": "was_live",
        "duration": 86_400,
        "formats": [{"url": "https://media.example/space.m3u8"}],
    }
    output = Path(tmp_path) / f"{SPACE_ID}.m4a"
    output.write_bytes(b"audio")
    monkeypatch.setattr(x_space.yt_dlp, "YoutubeDL", lambda _: downloader)
    monkeypatch.setattr(x_space, "MAX_AUDIO_BYTES", 0)

    with pytest.raises(x_space.XSpaceAudioUnavailableError, match="upload limit"):
        x_space._download(f"https://x.com/i/spaces/{SPACE_ID}", str(tmp_path))

    downloader.download.assert_called_once_with([f"https://x.com/i/spaces/{SPACE_ID}"])


def test_download_x_space_audio_preserves_conversion_errors(monkeypatch, tmp_path):
    from vsummary.util import x_space

    monkeypatch.setattr(x_space.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    downloader = MagicMock()
    downloader.__enter__.return_value = downloader
    downloader.extract_info.return_value = {
        "extractor_key": "TwitterSpaces",
        "id": SPACE_ID,
        "live_status": "was_live",
        "formats": [{"url": "https://media.example/space.m3u8"}],
    }
    downloader.download.side_effect = yt_dlp.utils.DownloadError(
        "ERROR: Postprocessing: Conversion failed!"
    )
    monkeypatch.setattr(x_space.yt_dlp, "YoutubeDL", lambda _: downloader)

    with pytest.raises(
        x_space.XSpaceAudioUnavailableError,
        match="Postprocessing: Conversion failed!",
    ):
        x_space._download(f"https://x.com/i/spaces/{SPACE_ID}", str(tmp_path))


def test_download_x_space_audio_requires_ffmpeg(monkeypatch, tmp_path):
    from vsummary.util import x_space

    monkeypatch.setattr(x_space.shutil, "which", lambda _: None)

    with pytest.raises(x_space.XSpaceAudioUnavailableError, match="ffmpeg"):
        x_space._download(f"https://x.com/i/spaces/{SPACE_ID}", str(tmp_path))
