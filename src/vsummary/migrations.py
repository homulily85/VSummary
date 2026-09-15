"""Explicit, additive MongoDB migrations for existing VSummary databases.

Migrations are deliberately not run by the bot startup path. Run
``vsummary-migrate --check`` before deployment, resolve any reported duplicate
keys, then run ``vsummary-migrate`` while the bot is stopped.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, AsyncMongoClient, IndexModel

from vsummary.settings import SettingsError, load_settings

CURRENT_SCHEMA_VERSION = 2
MIGRATION_RECORD_ID = "schema"

UNIQUE_KEYS = {
    "Channel": ("channel_id",),
    "PendingVideo": ("channel_id", "video_id"),
    "Video": ("source", "video_id"),
}


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """The migration's safe-to-display outcome."""

    duplicates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    modified_counts: dict[str, int] = field(default_factory=dict)
    check_only: bool = False


class MigrationDuplicateError(RuntimeError):
    """Raised before writes when old data would prevent a required unique index."""

    def __init__(self, duplicates: dict[str, list[dict[str, Any]]]):
        self.duplicates = duplicates
        collections = ", ".join(sorted(duplicates))
        super().__init__(
            "Duplicate records prevent required unique indexes in "
            f"{collections}. Resolve or merge these records manually, then rerun."
        )


async def inspect_database(database: Any) -> MigrationReport:
    """Report duplicate natural keys without changing user data."""
    duplicates: dict[str, list[dict[str, Any]]] = {}
    for collection_name, keys in UNIQUE_KEYS.items():
        group_keys = {key: f"${key}" for key in keys}
        # Version 1 stored a Video's identifier as ``id``. Inspect the value
        # it will have after the additive rename so a valid legacy database is
        # not falsely blocked by several missing ``video_id`` fields.
        if collection_name == "Video":
            group_keys["video_id"] = {"$ifNull": ["$video_id", "$id"]}
        cursor = database[collection_name].aggregate(
            [
                {
                    "$group": {
                        "_id": group_keys,
                        "count": {"$sum": 1},
                    }
                },
                {"$match": {"count": {"$gt": 1}}},
                {"$sort": {"count": -1}},
            ]
        )
        records = await cursor.to_list(length=None)
        if records:
            duplicates[collection_name] = records
    return MigrationReport(duplicates=duplicates, check_only=True)


async def migrate_database(
    database: Any, *, check_only: bool = False
) -> MigrationReport:
    """Apply schema version 2 safely and idempotently.

    A duplicate preflight always runs first. No records or indexes are changed
    when a future unique index would fail, avoiding partial production updates.
    """
    inspection = await inspect_database(database)
    if inspection.duplicates:
        raise MigrationDuplicateError(inspection.duplicates)
    if check_only:
        return inspection

    jobs = database["PendingVideo"]
    channels = database["Channel"]
    videos = database["Video"]
    now = datetime.now(UTC)
    modifications: dict[str, int] = {}

    async def update(collection, query: dict, update: dict, label: str) -> None:
        result = await collection.update_many(query, update)
        modifications[label] = modifications.get(label, 0) + getattr(
            result, "modified_count", 0
        )

    await update(jobs, {"status": "done"}, {"$set": {"status": "completed"}}, "jobs")
    await update(
        jobs,
        {"status": {"$exists": False}},
        {"$set": {"status": "queued"}},
        "jobs",
    )
    for field_name, default in (
        ("summary_details", []),
        ("delivery_retry_count", 0),
        ("delivery_chunks", []),
        ("delivery_chunk_index", 0),
        ("claimed_by", None),
        ("claimed_at", None),
        ("lease_expires_at", None),
        ("generated_at", None),
        ("delivered_at", None),
        ("last_error", None),
    ):
        await update(
            jobs,
            {field_name: {"$exists": False}},
            {"$set": {field_name: default}},
            "jobs",
        )
    await update(
        videos,
        {"id": {"$exists": True}, "video_id": {"$exists": False}},
        {"$rename": {"id": "video_id"}},
        "videos",
    )
    await update(
        channels,
        {"added_at": {"$exists": False}},
        {"$set": {"added_at": now}},
        "channels",
    )

    await channels.create_indexes(
        [IndexModel([("channel_id", ASCENDING)], unique=True)]
    )
    await jobs.create_indexes(
        [
            IndexModel(
                [("channel_id", ASCENDING), ("video_id", ASCENDING)], unique=True
            ),
            IndexModel([("status", ASCENDING)]),
            IndexModel([("next_attempt_at", ASCENDING)]),
            IndexModel([("lease_expires_at", ASCENDING)]),
        ]
    )
    await videos.create_indexes(
        [IndexModel([("source", ASCENDING), ("video_id", ASCENDING)], unique=True)]
    )
    await database["vsummary_migrations"].update_one(
        {"_id": MIGRATION_RECORD_ID},
        {
            "$max": {"version": CURRENT_SCHEMA_VERSION},
            "$setOnInsert": {"applied_at": now},
        },
        upsert=True,
    )
    return MigrationReport(duplicates={}, modified_counts=modifications)


async def async_main(*, check_only: bool = False) -> MigrationReport:
    """Run the independent migration command using normal runtime settings."""
    try:
        settings = load_settings()
    except SettingsError as exc:
        raise SystemExit(str(exc)) from exc

    client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True)
    try:
        return await migrate_database(
            client[settings.mongodb_database], check_only=check_only
        )
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate a VSummary MongoDB database")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only inspect for duplicate keys; do not write records or indexes",
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(async_main(check_only=args.check))
    except MigrationDuplicateError as exc:
        raise SystemExit(str(exc)) from exc
    action = "Preflight passed" if args.check else "Migration complete"
    print(f"{action}; changed fields: {report.modified_counts}")


if __name__ == "__main__":
    main()
