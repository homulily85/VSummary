"""Deletion of cached topic lists and details by video."""

from beanie.exceptions import CollectionWasNotInitialized

from vsummary.model.video import VideoSummary
from vsummary.model.wiki import VideoVerification
from vsummary.util.video import VideoRef


async def delete_cached_summary(ref: VideoRef) -> int:
    """Delete all summary caches for one source and video ID, including legacy records.

    Callers use the shared video lock to coordinate deletion with generation.
    For YouTube, also delete the video's verification snapshots when their
    collection is initialized. Keep the shared wiki evidence.

    Args:
        ref: Exact source and video identity to clear.

    Returns:
        Number of summary cache documents deleted across all languages.

    Raises:
        pymongo.errors.PyMongoError: If MongoDB cannot complete the deletion.
    """
    result = await VideoSummary.get_pymongo_collection().delete_many(
        {"source": ref.source, "video_id": ref.video_id}
    )
    if ref.source == "youtube":
        try:
            collection = VideoVerification.get_pymongo_collection()
        except CollectionWasNotInitialized:
            # Standalone callers may initialize only the summary collection.
            pass
        else:
            await collection.delete_many({"video_id": ref.video_id})
    return result.deleted_count
