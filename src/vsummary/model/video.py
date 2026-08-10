from typing import ClassVar

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import IndexModel


class Topic(BaseModel):
    name: str
    detail: str | None = None


class VideoSummary(Document):
    source: str
    video_id: str
    topics: list[Topic] = Field(default_factory=list)

    class Settings:
        name = "Video"
        indexes: ClassVar[list] = [
            IndexModel([("source", 1), ("video_id", 1)], unique=True)
        ]


class Video(Document):
    source: str
    video_id: str
    topics: list[Topic] | None = None
