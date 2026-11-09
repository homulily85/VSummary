"""Deletion of cached topic lists and details by video."""

from vsummary.model.video import VideoSummary
from vsummary.util.video import VideoRef


async def delete_cached_summary(ref: VideoRef) -> int:
    """Delete all summary caches for one source and video ID, including legacy records.

    Callers use the shared video lock to coordinate deletion with generation. The
    deletion targets the summary cache collection.

    Args:
        ref: Exact source and video identity to clear.

    Returns:
        Number of cache documents deleted across all languages.

    Raises:
        pymongo.errors.PyMongoError: If MongoDB cannot complete the deletion.
    """
    result = await VideoSummary.get_pymongo_collection().delete_many(
        {"source": ref.source, "video_id": ref.video_id}
    )
    return result.deleted_count
