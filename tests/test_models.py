from vsummary.model.channel import Channel, PendingVideoStatus
from vsummary.model.video import Video
from vsummary.util.holodex import MAX_RETRIES


class TestVideoModel:
    def test_replaces_link_with_source_and_video_id(self):
        fields = Video.model_fields
        assert "source" in fields
        assert "video_id" in fields
        assert "notebook_id" not in fields
        assert "link" not in fields


class TestChannelModel:
    def test_required_fields(self):
        fields = Channel.model_fields
        assert "channel_id" in fields
        assert "name" in fields
        assert "added_at" in fields


class TestPendingVideoModel:
    def test_status_enum_values(self):
        assert PendingVideoStatus.QUEUED == "queued"
        assert PendingVideoStatus.DONE == "done"
        assert PendingVideoStatus.FAILED == "failed"

    def test_retry_limit_aligns_with_model_default(self):
        assert MAX_RETRIES == 5
