import pytest

from vsummary.util.youtube import InvalidYouTubeInputError, to_video_url

VIDEO_ID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


@pytest.mark.parametrize(
    "value",
    [
        VIDEO_ID,
        f"  {VIDEO_ID}  ",
        WATCH_URL,
        f"https://youtube.com/watch?foo=bar&v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}/extra",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"HTTP://youtu.be/{VIDEO_ID}",
    ],
)
def test_to_video_url_normalizes_supported_inputs(value):
    assert to_video_url(value) == WATCH_URL


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-a-valid-id",
        "abc",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch",
        "https://youtube.com/watch?v=short",
        "https://youtube.com/",
    ],
)
def test_to_video_url_rejects_unsupported_inputs(value):
    with pytest.raises(InvalidYouTubeInputError):
        to_video_url(value)


def test_to_video_url_returns_a_string():
    assert isinstance(to_video_url(VIDEO_ID), str)
