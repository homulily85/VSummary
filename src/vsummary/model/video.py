from beanie import Document
from pydantic import BaseModel


class Topic(BaseModel):
    name: str
    detail: str | None = None


class Video(Document):
    source: str
    video_id: str
    notebook_id: str
    topics: list[Topic] | None = None
