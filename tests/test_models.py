import pytest
from pydantic import ValidationError

from vsummary.model.channel import Channel, PendingVideo, PendingVideoStatus
from vsummary.model.video import Topic, Video


def test_video_requires_source_and_video_id():
    fields = Video.model_fields

    assert fields["source"].is_required()
    assert fields["video_id"].is_required()
    assert fields["topics"].default is None


def test_video_contains_source_video_id_and_optional_topics():
    fields = Video.model_fields

    assert {"source", "video_id", "topics"} <= fields.keys()
    assert fields["topics"].annotation == list[Topic] | None
    assert Topic(name="Introduction").detail is None


def test_topic_detail_is_optional_but_name_is_required():
    assert Topic(name="Topic", detail="Details").detail == "Details"

    with pytest.raises(ValidationError):
        Topic()


def test_channel_requires_channel_id_and_name():
    fields = Channel.model_fields

    assert fields["channel_id"].is_required()
    assert fields["name"].is_required()


def test_pending_video_defaults_to_queued_with_zero_retries():
    fields = PendingVideo.model_fields

    assert fields["retry_count"].default == 0
    assert fields["status"].default is PendingVideoStatus.QUEUED
    assert fields["available_at"].is_required()
    assert fields["next_attempt_at"].is_required()


def test_pending_video_status_values_are_stable():
    assert [status.value for status in PendingVideoStatus] == [
        "queued",
        "done",
        "failed",
    ]
