from typing import Optional, List

from beanie import Document
from pydantic import BaseModel


class Topic(BaseModel):
    name: str
    index: int
    description: Optional[str] = None


class Video(Document):
    link: str
    notebook_id: str
    topics: Optional[List[Topic]] = []
