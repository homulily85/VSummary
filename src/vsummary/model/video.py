"""Cached NotebookLM topics and details stored by source and output language."""

from typing import ClassVar

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import IndexModel

VIDEO_LANGUAGE_CACHE_INDEX = IndexModel(
    [("source", 1), ("video_id", 1), ("output_language", 1)],
    unique=True,
    name="source_video_output_language_unique",
)


class Topic(BaseModel):
    """Represent one topic in the order it appears in a summary.

    Attributes:
        name: Display name used to identify the topic.
        detail: Generated explanation, or ``None`` before detail generation.
    """

    name: str
    detail: str | None = None


class VideoSummary(Document):
    """Store reusable topic output for one video and language.

    Attributes:
        source: Source key from the normalized video reference.
        video_id: Identifier within the source.
        output_language: Trimmed, case-folded language key. Untagged legacy records use
            ``None`` and are excluded from language-specific lookups.
        topics: Ordered topic list with any generated details.
    """

    source: str
    video_id: str
    output_language: str | None = None
    topics: list[Topic] = Field(default_factory=list)

    class Settings:
        """Configure the unique index for the source, video, and language cache key."""

        name = "VideoSummary"
        indexes: ClassVar[list] = [VIDEO_LANGUAGE_CACHE_INDEX]
