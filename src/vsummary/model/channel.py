"""Persistent channel subscriptions and durable summary-job state machines."""

from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar, Literal, Self

from beanie import Document
from pydantic import Field, field_validator, model_validator
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
    CANCELLED = "cancelled"


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
        name = "FollowedChannel"
        indexes: ClassVar[list] = [IndexModel([("channel_id", 1)], unique=True)]


class AutoSummaryTopicPolicy(Document):
    """One effective-dated decision to ignore or allow a Holodex topic.

    Multiple records for one ``topic_key`` preserve the policy history needed
    to decide based on a video's publication time rather than poll time.
    """

    topic_id: str
    topic_key: str
    ignored: bool
    effective_at: datetime

    @field_validator("topic_id", "topic_key")
    @classmethod
    def require_nonempty_topic_text(cls, value: str) -> str:
        """Strip and reject blank topic IDs and normalized lookup keys."""
        value = value.strip()
        if not value:
            raise ValueError("topic IDs must not be blank")
        return value

    @field_validator("effective_at")
    @classmethod
    def require_policy_timezone(cls, value: datetime) -> datetime:
        """Require UTC-normalizable policy activation timestamps."""
        if value.tzinfo is None:
            raise ValueError("effective_at must be timezone-aware")
        return value.astimezone(UTC)

    class Settings:
        name = "AutoSummaryTopicPolicy"
        indexes: ClassVar[list] = [
            IndexModel([("topic_key", 1), ("effective_at", 1)], unique=True)
        ]


class SummaryJob(Document):
    """Durable automatic or manual summary job with leased generation and delivery phases.

    The payload is saved before Discord delivery so transient send failures do
    not regenerate NotebookLM output. ``claimed_*`` fields implement a worker
    lease and ``delivery_chunk_index`` checkpoints partial delivery.
    ``youtube_audio_fallback`` selects the final audio attempt after native retries.
    """

    job_type: Literal["auto", "manual"]
    source: str = "youtube"
    video_id: str
    source_url: str | None = None
    channel_id: str | None = None
    discord_channel_id: int | None = None
    channel_name: str | None = None
    title: str | None = None
    available_at: datetime | None = None
    operation: ManualSummaryOperation | None = None
    topic_index: int | None = Field(default=None, ge=0)
    requester_id: int | None = None
    failure_notice: bool = False
    retry_count: int = 0
    youtube_audio_fallback: bool = False
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

    @model_validator(mode="after")
    def require_job_fields(self) -> Self:
        if self.job_type == "auto":
            if self.source != "youtube" or any(
                value is None
                for value in (
                    self.channel_id,
                    self.channel_name,
                    self.title,
                    self.available_at,
                )
            ):
                raise ValueError(
                    "auto jobs require YouTube channel metadata and available_at"
                )
        elif any(
            value is None
            for value in (self.operation, self.requester_id, self.discord_channel_id)
        ):
            raise ValueError(
                "manual jobs require operation, requester_id and discord_channel_id"
            )
        return self

    class Settings:
        name = "SummaryJob"
        indexes: ClassVar[list] = [
            IndexModel(
                [("channel_id", 1), ("video_id", 1)],
                unique=True,
                name="auto_channel_video_unique",
                partialFilterExpression={"job_type": "auto"},
            ),
            [("job_type", 1), ("status", 1), ("next_attempt_at", 1)],
            [("lease_expires_at", 1)],
        ]
