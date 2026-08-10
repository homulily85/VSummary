from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vsummary.model.channel import JobStatus, SummaryJob
from vsummary.model.video import VideoSummary
from vsummary.util.video import VideoRef


def test_refactored_models_use_non_optional_topics_and_explicit_states():
    summary = VideoSummary.model_construct(source="youtube", video_id="video")
    job = SummaryJob.model_construct(
        video_id="video",
        channel_id="channel",
        channel_name="Channel",
        title="Title",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )

    assert summary.topics == []
    assert job.status is JobStatus.QUEUED
    assert [status.value for status in JobStatus] == [
        "queued",
        "generating",
        "ready_to_deliver",
        "delivering",
        "completed",
        "failed",
    ]


def test_refactored_models_reject_naive_job_datetimes():
    with pytest.raises(ValidationError):
        SummaryJob(
            video_id="video",
            channel_id="channel",
            channel_name="Channel",
            title="Title",
            available_at=datetime.now(UTC).replace(tzinfo=None),
            next_attempt_at=datetime.now(UTC),
        )


def test_video_ref_accepts_explicit_video_id_and_keeps_read_only_compatibility():
    reference = VideoRef(source="youtube", video_id="abc")

    assert reference.video_id == "abc"
    assert reference.id == "abc"
    assert reference == VideoRef(source="youtube", id="abc")
