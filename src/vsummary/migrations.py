from __future__ import annotations

from typing import Any

CURRENT_SCHEMA_VERSION = 1
MIGRATION_RECORD_ID = "schema"


async def migrate_database(database: Any) -> None:
    """Apply additive, repeatable migrations without dropping user data."""
    migrations = database["vsummary_migrations"]
    record = await migrations.find_one({"_id": MIGRATION_RECORD_ID})
    if record and record.get("version", 0) >= CURRENT_SCHEMA_VERSION:
        return

    jobs = database["PendingVideo"]
    videos = database["Video"]

    await jobs.update_many(
        {"status": "done"},
        {"$set": {"status": "completed"}},
    )
    await jobs.update_many(
        {"summary_details": {"$exists": False}},
        {
            "$set": {
                "summary_details": [],
                "delivery_retry_count": 0,
            }
        },
    )
    await jobs.update_many(
        {"status": {"$exists": False}},
        {"$set": {"status": "queued"}},
    )
    await videos.update_many(
        {"id": {"$exists": True}, "video_id": {"$exists": False}},
        {"$rename": {"id": "video_id"}},
    )
    await migrations.update_one(
        {"_id": MIGRATION_RECORD_ID},
        {"$set": {"version": CURRENT_SCHEMA_VERSION}},
        upsert=True,
    )
