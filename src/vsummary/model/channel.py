"""MongoDB documents for channel subscriptions, topic policies, and summary jobs."""

from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar, Literal, Self

from beanie import Document
from pydantic import Field, field_validator, model_validator
from pymongo import IndexModel

from vsummary.model.video import Topic


class JobStatus(str, Enum):
    """Describe the queued, active, and terminal states of a durable summary job."""

    QUEUED = "queued"
    GENERATING = "generating"
    READY_TO_DELIVER = "ready_to_deliver"
    DELIVERING = "delivering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ManualSummaryOperation(str, Enum):
    """Select topics, one topic's details, or every topic's details for a manual job."""

    TOPICS = "topics"
    DETAIL_ONE = "detail_one"
    DETAIL_ALL = "detail_all"


class FollowedChannel(Document):
    """Store a Holodex channel followed for automatic summaries.

    Attributes:
        channel_id: Unique Holodex/YouTube channel identifier.
        name: Display name captured when the channel was followed.
        added_at: UTC subscription time used as the exclusive lower bound for polling.
    """

    channel_id: str
    name: str
    added_at: datetime

    @field_validator("added_at")
    @classmethod
    def require_added_at_timezone(cls, value: datetime) -> datetime:
        """Normalize the subscription timestamp to UTC.

        Args:
            value: Subscription timestamp supplied during validation.

        Returns:
            Timestamp representing the same instant in UTC.

        Raises:
            ValueError: If the timestamp has no timezone.
        """
        if value.tzinfo is None:
            raise ValueError("added_at must be timezone-aware")
        return value.astimezone(UTC)

    class Settings:
        """Configure the collection name and unique channel index."""

        name = "FollowedChannel"
        indexes: ClassVar[list] = [IndexModel([("channel_id", 1)], unique=True)]


class AutoSummaryTopicPolicy(Document):
    """Record a historical decision to allow or ignore a Holodex topic.

    Attributes:
        topic_id: Trimmed topic ID used for display.
        topic_key: Case-folded lookup key shared by all decisions for this topic.
        ignored: Whether to skip automatic summaries under this decision.
        effective_at: UTC activation time compared with the video's publication time.
    """

    topic_id: str
    topic_key: str
    ignored: bool
    effective_at: datetime

    @field_validator("topic_id", "topic_key")
    @classmethod
    def require_nonempty_topic_text(cls, value: str) -> str:
        """Validate text used to identify a policy topic.

        Args:
            value: Display ID or normalized lookup key.

        Returns:
            Value with surrounding whitespace removed.

        Raises:
            ValueError: If the trimmed text is empty.
        """
        value = value.strip()
        if not value:
            raise ValueError("topic IDs must not be blank")
        return value

    @field_validator("effective_at")
    @classmethod
    def require_policy_timezone(cls, value: datetime) -> datetime:
        """Normalize a policy's activation time to UTC.

        Args:
            value: Activation timestamp supplied during validation.

        Returns:
            Timestamp representing the same instant in UTC.

        Raises:
            ValueError: If the timestamp has no timezone.
        """
        if value.tzinfo is None:
            raise ValueError("effective_at must be timezone-aware")
        return value.astimezone(UTC)

    class Settings:
        """Index decisions by topic key and activation time, with one record per pair."""

        name = "AutoSummaryTopicPolicy"
        indexes: ClassVar[list] = [
            IndexModel([("topic_key", 1), ("effective_at", 1)], unique=True)
        ]


class SummaryJob(Document):
    """Persist one automatic or manual request across generation and Discord delivery.

    A worker lease coordinates processing. Saved chunks let delivery retry from the
    recorded position after a send failure.

    Attributes:
        job_type: Worker category, either ``auto`` or ``manual``.
        source: Media source key, defaulting to YouTube.
        video_id: Source-local video identifier or URL-derived generic media key.
        source_url: Stored URL needed to reopen generic media.
        channel_id: YouTube channel identifier required by automatic jobs.
        discord_channel_id: Destination Discord channel ID required by manual jobs.
        channel_name: Display channel name, required by automatic jobs.
        title: Video title, required by automatic jobs.
        wiki_metadata: Optional YouTube metadata snapshot from manual enqueue.
        available_at: UTC Holodex publication time for automatic jobs.
        operation: Requested output shape for a manual job.
        topic_index: Optional zero-based topic index for a single-topic request.
        requester_id: Discord user ID required by manual jobs.
        failure_notice: Whether the payload reports a terminal generation failure.
        retry_count: Number of generation failures recorded.
        youtube_audio_fallback: Whether the job should attempt YouTube audio.
        delivery_retry_count: Number of delivery failures recorded.
        next_attempt_at: Earliest UTC time at which the current queued phase is due.
        status: Current generation or delivery state.
        summary_details: Stored topic output associated with the job.
        last_error: Most recent recorded error, if any.
        claimed_by: ID of the worker that owns the lease.
        claimed_at: UTC time when the lease was acquired.
        lease_expires_at: UTC deadline after which the lease may be reclaimed.
        generated_at: UTC time when generation completed.
        delivered_at: UTC time when delivery completed.
        delivery_chunks: Saved Discord message chunks in send order.
        delivery_chunk_index: Zero-based position of the next unsent chunk.
    """

    job_type: Literal["auto", "manual"]
    source: str = "youtube"
    video_id: str
    source_url: str | None = None
    channel_id: str | None = None
    discord_channel_id: int | None = None
    channel_name: str | None = None
    title: str | None = None
    wiki_metadata: dict | None = None
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
        """Normalize a populated job timestamp to UTC.

        Args:
            value: Timestamp or ``None`` for an unset optional field.

        Returns:
            Timestamp in UTC, or ``None`` when unset.

        Raises:
            ValueError: If a timestamp has no timezone.
        """
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def require_job_fields(self) -> Self:
        """Validate the fields required by the job's worker category.

        Returns:
            The validated job instance.

        Raises:
            ValueError: If an automatic job lacks its YouTube source or required
                metadata, or a manual job lacks its operation, requester, or
                destination.
        """
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
        """Configure job uniqueness, due-job lookups, and lease-expiration indexes."""

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
