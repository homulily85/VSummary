from datetime import datetime
from enum import Enum

from beanie import Document


class PendingVideoStatus(str, Enum):
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"


class Channel(Document):
    channel_id: str
    name: str


class PendingVideo(Document):
    video_id: str
    channel_id: str
    channel_name: str
    title: str
    available_at: datetime
    retry_count: int = 0
    next_attempt_at: datetime
    status: PendingVideoStatus = PendingVideoStatus.QUEUED
