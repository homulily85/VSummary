"""Persistent cached NotebookLM topic data."""

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
    """One ordered video topic and its optional generated explanation."""

    name: str
    detail: str | None = None


class VideoSummary(Document):
    """Cached topics keyed by source, source-local ID, and normalized language.

    Untagged legacy records retain ``None`` and are excluded from new lookups.
    """

    source: str
    video_id: str
    output_language: str | None = None
    topics: list[Topic] = Field(default_factory=list)

    class Settings:
        """Store canonical summaries in the compatible ``Video`` collection."""

        name = "Video"
        indexes: ClassVar[list] = [VIDEO_LANGUAGE_CACHE_INDEX]


class Video(Document):
    """Legacy compatibility shape for cached video documents."""

    source: str
    video_id: str
    topics: list[Topic] | None = None
