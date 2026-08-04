import pytest

from vsummary.util.youtube import InvalidYouTubeInputError, to_video_url


class TestToVideoUrl:
    @pytest.mark.parametrize(
        "value",
        [
            "dQw4w9WgXcQ",
            "  dQw4w9WgXcQ  ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?foo=bar&v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ/extra",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "HTTP://youtu.be/dQw4w9WgXcQ",
        ],
    )
    def test_valid_inputs(self, value):
        assert to_video_url(value) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not-a-valid-11-char-id",
            "abc",
            "https://example.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch",
            "https://youtube.com/watch?v=short",
            "https://youtube.com/",
        ],
    )
    def test_invalid_inputs(self, value):
        with pytest.raises(InvalidYouTubeInputError):
            to_video_url(value)

    def test_returns_str_type(self):
        assert isinstance(to_video_url("dQw4w9WgXcQ"), str)
