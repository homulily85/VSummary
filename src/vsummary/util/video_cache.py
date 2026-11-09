"""Cached summary deletion."""

from vsummary.model.video import VideoSummary
from vsummary.util.video import VideoRef


async def delete_cached_summary(ref: VideoRef) -> int:
    """Delete all language and legacy caches for one exact source/video identity.

    Callers coordinate with generation using the shared per-video lock. Durable
    jobs and their saved delivery payloads are stored separately and retained.
    """
    result = await VideoSummary.get_pymongo_collection().delete_many(
        {"source": ref.source, "video_id": ref.video_id}
    )
    return result.deleted_count
