"""Persistent channel subscriptions and durable summary-job state machines."""

from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar

from beanie import Document
from pydantic import Field, field_validator
from pymongo import IndexModel

from vsummary.model.video import Topic


class JobStatus(str, Enum):
    """Lifecycle states shared by automatic and manual summary jobs."""

    QUEUED = "queued"
    GENERATING = "generating"
    READY_TO_DELIVER = "ready_to_deliver"
    DELIVERING = "delivering"
    COMPLETED = "completed"
    FAILED = "failed"


class ManualSummaryOperation(str, Enum):
    """Requested output shape for a manual summary job."""

    TOPICS = "topics"
    DETAIL_ONE = "detail_one"
    DETAIL_ALL = "detail_all"


class FollowedChannel(Document):
    """A Holodex channel that should receive automatic summary coverage.

    Attributes:
        channel_id: Holodex/YouTube channel identifier.
        name: Display name captured when the subscription was created.
        added_at: UTC time used as the lower bound for new-video polling.
    """

    channel_id: str
    name: str
    added_at: datetime

    @field_validator("added_at")
    @classmethod
    def require_added_at_timezone(cls, value: datetime) -> datetime:
        """Reject naive timestamps and persist the channel start time in UTC."""
        if value.tzinfo is None:
            raise ValueError("added_at must be timezone-aware")
        return value.astimezone(UTC)

    class Settings:
        """Map this model to the legacy-preserved ``Channel`` collection."""

        name = "Channel"
        indexes: ClassVar[list] = [IndexModel([("channel_id", 1)], unique=True)]


class SummaryJob(Document):
    """Durable automatic-summary job with leased generation and delivery phases.

    The payload is saved before Discord delivery so transient send failures do
    not regenerate NotebookLM output. ``claimed_*`` fields implement a worker
    lease and ``delivery_chunk_index`` checkpoints partial delivery.
    """

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
        """Normalize optional job timestamps to UTC and reject naive values."""
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None

    class Settings:
        """Map to the compatible ``PendingVideo`` collection and its indexes."""

        name = "PendingVideo"
        indexes: ClassVar[list] = [
            IndexModel([("channel_id", 1), ("video_id", 1)], unique=True),
            [("status", 1)],
            [("next_attempt_at", 1)],
            [("lease_expires_at", 1)],
        ]


class ManualSummaryJob(Document):
    """Durable user-requested job targeting the Discord channel that requested it.

    ``operation`` and ``topic_index`` describe the requested content, while
    retry, lease, and delivery fields mirror :class:`SummaryJob` semantics.
    """

    source: str
    video_id: str
    title: str | None = None
    channel_name: str | None = None
    operation: ManualSummaryOperation
    topic_index: int | None = None
    channel_id: int
    requester_id: int
    next_attempt_at: datetime
    status: JobStatus = JobStatus.QUEUED
    retry_count: int = 0
    delivery_retry_count: int = 0
    delivery_chunks: list[str] = Field(default_factory=list)
    delivery_chunk_index: int = 0
    last_error: str | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None

    @field_validator("next_attempt_at", "claimed_at", "lease_expires_at")
    @classmethod
    def require_manual_job_timezone(cls, value: datetime | None) -> datetime | None:
        """Normalize manual-job lease and schedule timestamps to UTC."""
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None

    class Settings:
        """Map manual jobs to their dedicated collection and worker indexes."""

        name = "ManualSummaryJob"
        indexes: ClassVar[list] = [
            [("status", 1), ("next_attempt_at", 1)],
            [("lease_expires_at", 1)],
        ]


class PendingVideoStatus(str, Enum):
    """Legacy automatic-summary status values retained for compatibility."""

    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"


Channel = FollowedChannel


class PendingVideo(Document):
    """Compatibility model for older pending-video documents and integrations."""

    video_id: str
    channel_id: str
    channel_name: str
    title: str
    available_at: datetime
    retry_count: int = 0
    next_attempt_at: datetime
    status: PendingVideoStatus = PendingVideoStatus.QUEUED
