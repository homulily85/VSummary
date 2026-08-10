from unittest.mock import AsyncMock, MagicMock

import pytest

from vsummary.migrations import CURRENT_SCHEMA_VERSION, migrate_database


@pytest.mark.asyncio
async def test_migrate_database_maps_legacy_job_states_and_records_version():
    database = MagicMock()
    jobs = MagicMock()
    videos = MagicMock()
    migrations = MagicMock()
    database.__getitem__.side_effect = {
        "PendingVideo": jobs,
        "Video": videos,
        "vsummary_migrations": migrations,
    }.__getitem__
    migrations.find_one = AsyncMock(return_value=None)
    migrations.update_one = AsyncMock()
    jobs.update_many = AsyncMock()
    videos.update_many = AsyncMock()

    await migrate_database(database)

    jobs.update_many.assert_any_await(
        {"status": "done"}, {"$set": {"status": "completed"}}
    )
    jobs.update_many.assert_any_await(
        {"summary_details": {"$exists": False}},
        {
            "$set": {
                "summary_details": [],
                "delivery_retry_count": 0,
            }
        },
    )
    migrations.update_one.assert_awaited_once()
    assert (
        migrations.update_one.await_args.args[1]["$set"]["version"]
        == CURRENT_SCHEMA_VERSION
    )


@pytest.mark.asyncio
async def test_migrate_database_is_idempotent_for_current_version():
    database = MagicMock()
    migrations = MagicMock()
    database.__getitem__.return_value = migrations
    migrations.find_one = AsyncMock(return_value={"version": CURRENT_SCHEMA_VERSION})
    migrations.update_one = AsyncMock()

    await migrate_database(database)

    migrations.update_one.assert_not_awaited()
