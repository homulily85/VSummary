from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from vsummary.migrations import MigrationDuplicateError, migrate_database


class Cursor:
    def __init__(self, values):
        self.values = values

    async def to_list(self, length=None):
        return self.values


def make_database(*, duplicates=None):
    duplicates = duplicates or {}
    database = MagicMock()
    collections = {
        name: MagicMock()
        for name in ("Channel", "PendingVideo", "Video", "vsummary_migrations")
    }
    database.__getitem__.side_effect = collections.__getitem__
    for name, collection in collections.items():
        collection.aggregate.return_value = Cursor(duplicates.get(name, []))
        collection.update_many = AsyncMock(
            return_value=SimpleNamespace(modified_count=0)
        )
        collection.update_one = AsyncMock()
        collection.create_indexes = AsyncMock()
    return database, collections


@pytest.mark.asyncio
async def test_migration_maps_legacy_jobs_adds_durable_fields_and_creates_indexes():
    database, collections = make_database()

    report = await migrate_database(database)

    jobs = collections["PendingVideo"]
    jobs.update_many.assert_any_await(
        {"status": "done"}, {"$set": {"status": "completed"}}
    )
    jobs.update_many.assert_any_await(
        {"delivery_chunks": {"$exists": False}},
        {"$set": {"delivery_chunks": []}},
    )
    jobs.update_many.assert_any_await(
        {"lease_expires_at": {"$exists": False}},
        {"$set": {"lease_expires_at": None}},
    )
    assert report.duplicates == {}
    assert jobs.create_indexes.await_count == 1
    collections["vsummary_migrations"].update_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_migration_stops_before_writing_when_unique_index_duplicates_exist():
    database, collections = make_database(
        duplicates={
            "PendingVideo": [
                {"_id": {"channel_id": "channel", "video_id": "video"}, "count": 2}
            ]
        }
    )

    with pytest.raises(MigrationDuplicateError, match="PendingVideo"):
        await migrate_database(database)

    collections["PendingVideo"].update_many.assert_not_awaited()
    collections["PendingVideo"].create_indexes.assert_not_awaited()


@pytest.mark.asyncio
async def test_migration_preflight_uses_legacy_video_id_when_checking_duplicates():
    database, collections = make_database()

    await migrate_database(database, check_only=True)

    video_pipeline = collections["Video"].aggregate.call_args.args[0]
    assert video_pipeline[0]["$group"]["_id"]["video_id"] == {
        "$ifNull": ["$video_id", "$id"]
    }
