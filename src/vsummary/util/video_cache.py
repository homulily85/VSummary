"""Startup index migration for language-specific video summary caches."""

import logging

from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import OperationFailure

from vsummary.model.video import VIDEO_LANGUAGE_CACHE_INDEX

logger = logging.getLogger(__name__)


async def migrate_video_cache_indexes(collection: AsyncCollection) -> None:
    """Establish the language key before removing obsolete video-only uniqueness.

    Keep cached documents and unrelated indexes intact. Only IndexNotFound is
    harmless when another startup concurrently removes the same obsolete index.
    All other failures abort startup before any workers run.
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
