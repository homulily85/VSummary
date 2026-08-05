import pytest

from vsummary.util.video import (
    UnsupportedVideoSource,
    VideoRef,
    build_video_url,
    parse_video_source,
)


class TestParseVideoRef:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?foo=bar&v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        ],
    )
    def test_youtube_sources(self, url):
        ref = parse_video_source(url)
        assert ref.source == "youtube"
        assert ref.id == "dQw4w9WgXcQ"

    @pytest.mark.parametrize(
        "url",
        [
            "https://vimeo.com/12345",
            "https://www.twitch.tv/videos/12345",
            "not-a-url",
            "",
        ],
    )
    def test_unsupported_sources(self, url):
        with pytest.raises(UnsupportedVideoSource):
            parse_video_source(url)


class TestBuildVideoUrl:
    def test_youtube(self):
        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        assert build_video_url(ref) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_unsupported_source(self):
        ref = VideoRef(source="vimeo", id="12345")
        with pytest.raises(UnsupportedVideoSource):
            build_video_url(ref)

    def test_round_trip(self):
        ref = parse_video_source("https://youtu.be/dQw4w9WgXcQ")
        assert parse_video_source(build_video_url(ref)) == ref
