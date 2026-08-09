from vsummary.model.video import Video


class TestVideoModel:
    def test_replaces_link_with_source_and_video_id(self):
        fields = Video.model_fields
        assert "source" in fields
        assert "video_id" in fields
        assert "notebook_id" not in fields
        assert "link" not in fields
