"""Cached summary deletion and offline index migration."""

import logging

from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import OperationFailure

from vsummary.model.video import VIDEO_LANGUAGE_CACHE_INDEX, VideoSummary
from vsummary.util.video import VideoRef

logger = logging.getLogger(__name__)


async def delete_cached_summary(ref: VideoRef) -> int:
    """Delete all language and legacy caches for one exact source/video identity.

    Callers coordinate with generation using the shared per-video lock. Durable
    jobs and their saved delivery payloads are stored separately and retained.
    """
    result = await VideoSummary.get_pymongo_collection().delete_many(
        {"source": ref.source, "video_id": ref.video_id}
    )
    return result.deleted_count


async def migrate_video_cache_indexes(collection: AsyncCollection) -> None:
    """Establish the language key before removing obsolete video-only uniqueness.

    Keep cached documents and unrelated indexes intact. Only IndexNotFound is
    harmless when another migration concurrently removes the same obsolete index.
    All other failures propagate to the offline migration command.
    """
    await collection.create_indexes([VIDEO_LANGUAGE_CACHE_INDEX])
    indexes = await collection.index_information()
    for name, index in indexes.items():
        if index.get("unique") and list(index.get("key", [])) == [
            ("source", 1),
            ("video_id", 1),
        ]:
            try:
                await collection.drop_index(name)
            except OperationFailure as exc:
                if exc.code != 27:  # MongoDB IndexNotFound after a concurrent startup.
                    raise
            logger.info("Removed obsolete video cache index %s.", name)
