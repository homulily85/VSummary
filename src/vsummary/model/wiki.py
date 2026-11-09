"""Internal evidence, identities, and per-video verification snapshots."""

from datetime import date, datetime
from typing import ClassVar, Literal

from beanie import Document
from pydantic import BaseModel, Field, model_validator
from pymongo import IndexModel

EntityKind = Literal["person", "channel", "organization", "product", "event"]
WikiStatus = Literal["candidate", "verified", "conflicted"]


class Validity(BaseModel):
    """Represent the period in which evidence supports a wiki fact.

    Attributes:
        start: Inclusive start date. An unknown start cannot establish
            that a fact applies to a particular date.
        end: Exclusive end date, if the period has a known end.
        precision: Granularity of the dates supplied by the evidence.
    """

    start: date | None = None
    end: date | None = None
    precision: Literal["day", "month", "year"] = "day"

    @model_validator(mode="after")
    def ordered_bounds(self):
        """Validate that populated bounds define a nonempty interval.

        Returns:
            The validated interval.

        Raises:
            ValueError: If the end is on or before the start.
        """
        if self.start and self.end and self.start >= self.end:
            raise ValueError("validity end must follow its start")
        return self

    def covers(self, at: date | None) -> bool:
        """Check whether the interval supports a fact on a given date.

        Args:
            at: Date to check, or None when it is unknown.

        Returns:
            Whether the date is on or after the known start and before any end.
        """
        return (
            at is not None
            and self.start is not None
            and self.start <= at
            and (self.end is None or at < self.end)
        )


class WikiEntity(Document):
    """Store the identity of an entity shared across videos.

    Attributes:
        id: Deterministic MongoDB ID derived from the external key.
        kind: Category used to distinguish different kinds of entities.
        name: Display name when the entity was first recorded.
        external_key: Source-backed key used to identify the entity.
        recorded_at: UTC time when the entity was first stored.
    """

    id: str
    kind: EntityKind
    name: str
    external_key: str
    recorded_at: datetime

    class Settings:
        """Index entities by their unique external key."""

        indexes: ClassVar[list] = [IndexModel("external_key", unique=True)]


class WikiAlias(Document):
    """Store an attested name and the scope in which it can be reused.

    Attributes:
        id: Deterministic ID for this alias and its evidence.
        entity_id: Entity the name refers to.
        kind: Entity category used for lookup.
        text: Original spelling of the name.
        key: Normalized lookup key.
        channel_id: Optional YouTube channel scope.
        speaker_id: Optional speaker scope.
        language: Language of the alias, when recorded.
        validity: Period during which the name applies.
        status: Verification state of this alias.
        evidence_ids: References to the supporting source excerpts.
        recorded_at: UTC time when the alias was recorded.
        supersedes: ID of an earlier version, if one was replaced.
    """

    id: str
    entity_id: str
    kind: EntityKind
    text: str
    key: str
    channel_id: str | None = None
    speaker_id: str | None = None
    language: str | None = None
    validity: Validity = Field(default_factory=Validity)
    status: WikiStatus
    evidence_ids: list[str]
    recorded_at: datetime
    supersedes: str | None = None

    class Settings:
        """Index aliases for name, scope, and entity lookups."""

        indexes: ClassVar[list] = [
            IndexModel([("key", 1), ("kind", 1), ("channel_id", 1), ("status", 1)]),
            IndexModel("entity_id"),
        ]


class WikiFact(Document):
    """Store a dated name or relationship with supporting evidence.

    Attributes:
        id: Deterministic ID for the fact and its evidence.
        subject_id: Entity the fact describes.
        relation: Kind of name, property, or relationship recorded.
        value: Recorded text or the ID of the related entity.
        validity: Period for which the fact is supported.
        status: Verification state of this fact.
        evidence_ids: References to supporting excerpts.
        recorded_at: UTC time when the fact was recorded.
        supersedes: ID of an earlier version, if one was replaced.
    """

    id: str
    subject_id: str
    relation: str
    value: str
    validity: Validity = Field(default_factory=Validity)
    status: WikiStatus
    evidence_ids: list[str]
    recorded_at: datetime
    supersedes: str | None = None

    class Settings:
        """Index facts by subject, relationship, status, and value."""

        indexes: ClassVar[list] = [
            IndexModel([("subject_id", 1), ("relation", 1), ("status", 1)]),
            IndexModel([("relation", 1), ("value", 1)]),
        ]


class WikiEvidence(Document):
    """Preserve an excerpt used to support a wiki record.

    Attributes:
        id: Deterministic ID derived from the URL, fingerprint, and excerpt.
        url: URL from which the excerpt was taken.
        excerpt: Source passage retained for traceability.
        content_hash: Fingerprint of the source text checked.
        observed_at: UTC time when the evidence was recorded.
        video_id: Video whose processing produced this evidence, when known.
    """

    id: str
    url: str
    excerpt: str
    content_hash: str
    observed_at: datetime
    video_id: str | None = None


class Mention(BaseModel):
    """Track a transcript name and the result of attempting to resolve it.

    Attributes:
        original: Name as it appears in the transcript.
        quote: Verbatim transcript context containing the name.
        kind: Category of entity being mentioned.
        at: Explicit date from the quoted text, if available.
        candidate_ids: Wiki entities considered during lookup.
        canonical: Supported canonical name, if one was found.
        entity_id: Linked wiki entity, if resolved to a shared record.
        evidence_ids: References to the evidence for the resolution.
    """

    original: str = Field(min_length=1, max_length=120)
    quote: str = Field(min_length=1, max_length=420)
    kind: EntityKind
    at: date | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    canonical: str | None = None
    entity_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        """Return whether the mention has a canonical name and supporting evidence."""
        return bool(self.canonical and self.evidence_ids)


class VideoVerification(Document):
    """Store the internal verification snapshot for a video.

    Attributes:
        id: Deterministic ID for the transcript, context, and results.
        video_id: YouTube ID of the verified video.
        pipeline_version: Version of the verification workflow.
        transcript_hash: Fingerprint of the imported transcript.
        metadata: Video metadata available to the verifier.
        queries: Research queries attempted for the video.
        mentions: Extracted names and their verification results.
        recorded_at: UTC time when the snapshot was stored.
    """

    id: str
    video_id: str
    pipeline_version: int
    transcript_hash: str
    metadata: dict
    queries: list[str]
    mentions: list[Mention]
    recorded_at: datetime

    class Settings:
        """Index verification snapshots for lookup and deletion by video."""

        indexes: ClassVar[list] = [IndexModel("video_id")]
