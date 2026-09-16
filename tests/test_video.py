import pytest

from vsummary.util.video import (
    UnsupportedVideoSource,
    VideoRef,
    build_video_url,
    parse_video_source,
)

VIDEO_ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "value",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?foo=bar&v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        VIDEO_ID,
    ],
)
def test_parse_video_source_returns_normalized_youtube_reference(value):
    assert parse_video_source(value) == VideoRef(source="youtube", id=VIDEO_ID)


@pytest.mark.parametrize("value", ["", "   ", "not-a-url", "https://vimeo.com/12345"])
def test_parse_video_source_rejects_unsupported_sources(value):
    with pytest.raises(UnsupportedVideoSource):
        parse_video_source(value)


def test_build_video_url_supports_youtube():
    ref = VideoRef(source="youtube", id=VIDEO_ID)

    assert build_video_url(ref) == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_build_video_url_rejects_unknown_sources():
    with pytest.raises(UnsupportedVideoSource):
        build_video_url(VideoRef(source="vimeo", id="12345"))


def test_parse_and_build_round_trip():
    ref = parse_video_source(f"https://youtu.be/{VIDEO_ID}")

    assert parse_video_source(build_video_url(ref)) == ref


@pytest.mark.parametrize(
    "value",
    [
        "https://www.twitch.tv/videos/123456789",
        "https://twitch.tv/videos/123456789?filter=archives",
    ],
)
def test_parse_video_source_accepts_full_twitch_vod_urls(value):
    assert parse_video_source(value) == VideoRef(source="twitch", video_id="123456789")


@pytest.mark.parametrize(
    "value",
    [
        "123456789",
        "https://clips.twitch.tv/ExampleClip",
        "https://www.twitch.tv/example_channel",
        "https://www.twitch.tv/videos/not-a-number",
    ],
)
def test_parse_video_source_rejects_unsupported_twitch_inputs(value):
    with pytest.raises(UnsupportedVideoSource):
        parse_video_source(value)


def test_build_video_url_supports_twitch_vods():
    assert build_video_url(VideoRef(source="twitch", video_id="123456789")) == (
        "https://www.twitch.tv/videos/123456789"
    )
