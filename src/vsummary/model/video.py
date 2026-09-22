"""Persistent cached NotebookLM topic data."""

from typing import ClassVar

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import IndexModel


class Topic(BaseModel):
    """One ordered video topic and its optional generated explanation."""

    name: str
    detail: str | None = None


class VideoSummary(Document):
    """Canonical cached topics keyed by video source and source-local ID."""

    source: str
    video_id: str
    topics: list[Topic] = Field(default_factory=list)

    class Settings:
        """Store canonical summaries in the compatible ``Video`` collection."""

        name = "Video"
        indexes: ClassVar[list] = [
            IndexModel([("source", 1), ("video_id", 1)], unique=True)
        ]


class Video(Document):
    """Legacy compatibility shape for cached video documents."""

    source: str
    video_id: str
    topics: list[Topic] | None = None
