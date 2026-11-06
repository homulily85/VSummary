from datetime import UTC, datetime

import pytest
from bson import ObjectId

from vsummary.migrations.v1_6 import MigrationError, normalize_document, plan_migration

NOW = datetime.now(UTC)


def auto(**values):
    return {
        "_id": ObjectId(),
        "video_id": "video",
        "channel_id": "UC1",
        "channel_name": "Name",
        "title": "Title",
        "available_at": NOW,
        "next_attempt_at": NOW,
        **values,
    }


def test_old_auto_status_and_delivery_progress_are_preserved():
    raw = auto(
        status="done",
        retry_count=3,
        delivery_chunks=["first", "second"],
        delivery_chunk_index=1,
    )
    job = normalize_document("PendingVideo", raw)
    assert job["_id"] == raw["_id"]
    assert job["job_type"] == "auto"
    assert job["status"] == "completed"
    assert job["delivery_chunk_index"] == 1
    assert job["retry_count"] == 3


def test_manual_destination_and_lease_survive_conversion():
    raw = {
        "_id": ObjectId(),
        "source": "twitch",
        "video_id": "123",
        "channel_id": 123,
        "operation": "topics",
        "requester_id": 456,
        "next_attempt_at": NOW,
        "status": "delivering",
        "claimed_by": "worker",
        "lease_expires_at": NOW,
        "delivery_chunks": ["text"],
    }
    job = normalize_document("ManualSummaryJob", raw)
    assert job["job_type"] == "manual"
    assert job["discord_channel_id"] == 123
    assert job["channel_id"] is None
    assert job["claimed_by"] == "worker"
    assert job["delivery_chunks"] == ["text"]


def test_untagged_cache_and_null_topics():
    cache = normalize_document(
        "Video",
        {"_id": ObjectId(), "source": "youtube", "video_id": "video", "topics": None},
    )
    assert cache["topics"] == []
    assert cache["output_language"] is None


def test_preflight_is_repeatable_and_accepts_partial_named_collections():
    old = auto()
    target = normalize_document("PendingVideo", old)
    plan = plan_migration({"PendingVideo": [old], "SummaryJob": [target]})
    assert plan.writes == []
    partial = plan_migration({"SummaryJob": [old]})
    assert partial.writes[0].document["job_type"] == "auto"


def test_conflict_does_not_overwrite_progress_or_duplicate_auto_identity():
    old = auto()
    target = normalize_document("PendingVideo", old)
    target["retry_count"] = 10
    with pytest.raises(MigrationError, match="conflict"):
        plan_migration({"PendingVideo": [old], "SummaryJob": [target]})
    with pytest.raises(MigrationError, match="duplicate"):
        plan_migration({"PendingVideo": [old, auto()]})


def test_multiple_manual_requests_for_same_video_are_allowed():
    raw = {
        "source": "youtube",
        "video_id": "video",
        "channel_id": 123,
        "requester_id": 456,
        "operation": "topics",
        "next_attempt_at": NOW,
    }
    plan = plan_migration(
        {"ManualSummaryJob": [{"_id": ObjectId(), **raw}, {"_id": ObjectId(), **raw}]}
    )
    assert len(plan.writes) == 2


def test_invalid_schema_and_status_fail_preflight():
    with pytest.raises(MigrationError):
        plan_migration({"PendingVideo": [auto(status="unknown")]})
    with pytest.raises(MigrationError):
        plan_migration({"PendingVideo": [{"_id": ObjectId()}]})
