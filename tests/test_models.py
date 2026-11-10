from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vsummary.model.channel import FollowedChannel, JobStatus, SummaryJob
from vsummary.model.video import Topic, VideoSummary


def test_video_requires_source_and_video_id():
    fields = VideoSummary.model_fields
    assert fields["source"].is_required()
    assert fields["video_id"].is_required()
    summary = VideoSummary.model_construct()
    assert summary.topics == []
    assert summary.output_language is None


def test_topic_detail_is_optional_but_name_is_required():
    assert Topic(name="Introduction").detail is None
    assert Topic(name="Topic", detail="Details").detail == "Details"
    with pytest.raises(ValidationError):
        Topic()


def test_channel_requires_channel_id_name_and_added_at():
    fields = FollowedChannel.model_fields
    assert all(
        fields[name].is_required() for name in ("channel_id", "name", "added_at")
    )


def test_job_defaults_and_lifecycle_states():
    job = SummaryJob.model_construct(
        job_type="auto",
        video_id="video",
        channel_id="channel",
        channel_name="Channel",
        title="Title",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )

    assert job.retry_count == 0
    assert job.youtube_audio_fallback is False
    assert SummaryJob.model_fields["next_attempt_at"].is_required()
    assert job.status is JobStatus.QUEUED
    assert job.delivery_chunks == []
    assert job.topic_count is None
    assert job.delivery_chunk_index == 0
    assert job.lease_expires_at is None
    assert [status.value for status in JobStatus] == [
        "queued",
        "generating",
        "ready_to_deliver",
        "delivering",
        "completed",
        "failed",
        "cancelled",
    ]


def test_video_summary_cache_has_a_unique_key_per_language():
    summary = VideoSummary.model_construct(
        source="youtube", video_id="video", output_language="japanese"
    )
    assert summary.output_language == "japanese"
    index = VideoSummary.Settings.indexes[0].document
    assert list(index["key"].items()) == [
        ("source", 1),
        ("video_id", 1),
        ("output_language", 1),
    ]
    assert index["unique"] is True


def test_job_rejects_naive_datetimes():
    with pytest.raises(ValidationError, match="timezone-aware"):
        SummaryJob(
            job_type="auto",
            video_id="video",
            channel_id="channel",
            channel_name="Channel",
            title="Title",
            available_at=datetime.now(UTC).replace(tzinfo=None),
            next_attempt_at=datetime.now(UTC),
        )


def validate(**values):
    # Validate without Beanie collection initialization (no database is needed).
    job = SummaryJob.model_construct()
    return SummaryJob.__pydantic_validator__.validate_python(values, self_instance=job)


def test_job_requires_explicit_kind():
    with pytest.raises(ValidationError, match="job_type"):
        validate(video_id="video", next_attempt_at=datetime.now(UTC))


def test_manual_job_requires_delivery_identity_and_operation():
    with pytest.raises(ValidationError, match="manual"):
        validate(
            job_type="manual",
            source="youtube",
            video_id="video",
            next_attempt_at=datetime.now(UTC),
        )
    job = validate(
        job_type="manual",
        source="twitch",
        video_id="video",
        operation="topics",
        discord_channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
    )
    assert job.channel_id is None
    assert job.discord_channel_id == 123


def test_auto_job_requires_youtube_metadata():
    values = {
        "job_type": "auto",
        "video_id": "video",
        "next_attempt_at": datetime.now(UTC),
    }
    with pytest.raises(ValidationError, match="auto"):
        validate(**values)
    values.update(
        channel_id="UC1",
        channel_name="Name",
        title="Title",
        available_at=datetime.now(UTC),
    )
    assert validate(**values).source == "youtube"
    with pytest.raises(ValidationError, match="auto"):
        validate(**values, source="twitch")


def test_auto_uniqueness_does_not_apply_to_manual_jobs():
    index = SummaryJob.Settings.indexes[0].document
    assert index["unique"] is True
    assert index["partialFilterExpression"] == {"job_type": "auto"}
