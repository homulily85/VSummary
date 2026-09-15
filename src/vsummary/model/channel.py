from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar

from beanie import Document
from pydantic import Field, field_validator
from pymongo import IndexModel

from vsummary.model.video import Topic


class JobStatus(str, Enum):
    QUEUED = "queued"
    GENERATING = "generating"
    READY_TO_DELIVER = "ready_to_deliver"
    DELIVERING = "delivering"
    COMPLETED = "completed"
    FAILED = "failed"


class FollowedChannel(Document):
    channel_id: str
    name: str
    added_at: datetime

    @field_validator("added_at")
    @classmethod
    def require_added_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("added_at must be timezone-aware")
        return value.astimezone(UTC)

    class Settings:
        name = "Channel"
        indexes: ClassVar[list] = [IndexModel([("channel_id", 1)], unique=True)]


class SummaryJob(Document):
    video_id: str
    channel_id: str
    channel_name: str
    title: str
    available_at: datetime
    retry_count: int = 0
    delivery_retry_count: int = 0
    next_attempt_at: datetime
    status: JobStatus = JobStatus.QUEUED
    summary_details: list[Topic] = Field(default_factory=list)
    last_error: str | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    generated_at: datetime | None = None
    delivered_at: datetime | None = None
    delivery_chunks: list[str] = Field(default_factory=list)
    delivery_chunk_index: int = 0

    @field_validator(
        "available_at",
        "next_attempt_at",
        "claimed_at",
        "lease_expires_at",
        "generated_at",
        "delivered_at",
    )
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None

    class Settings:
        name = "PendingVideo"
        indexes: ClassVar[list] = [
            IndexModel([("channel_id", 1), ("video_id", 1)], unique=True),
            [("status", 1)],
            [("next_attempt_at", 1)],
            [("lease_expires_at", 1)],
        ]


class PendingVideoStatus(str, Enum):
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"


Channel = FollowedChannel


class PendingVideo(Document):
    video_id: str
    channel_id: str
    channel_name: str
    title: str
    available_at: datetime
    retry_count: int = 0
    next_attempt_at: datetime
    status: PendingVideoStatus = PendingVideoStatus.QUEUED
