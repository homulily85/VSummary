"""Offline migration to 1.6. Stop all bot processes before running this module."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError
from pymongo import AsyncMongoClient, IndexModel
from pymongo.errors import OperationFailure, PyMongoError

from vsummary.model.channel import FollowedChannel, SummaryJob
from vsummary.model.video import VIDEO_LANGUAGE_CACHE_INDEX, VideoSummary
from vsummary.settings import DATABASE_NAME

logger = logging.getLogger(__name__)

TARGETS = {
    "Channel": "FollowedChannel",
    "PendingVideo": "SummaryJob",
    "ManualSummaryJob": "SummaryJob",
    "Video": "VideoSummary",
}
MODELS = {
    "FollowedChannel": FollowedChannel,
    "SummaryJob": SummaryJob,
    "VideoSummary": VideoSummary,
}


class MigrationError(ValueError):
    """Unsafe or invalid migration input; no conflicting record is overwritten."""


@dataclass
class Write:
    collection: str
    document: dict[str, Any]
    original: dict[str, Any] | None


@dataclass
class MigrationPlan:
    writes: list[Write] = field(default_factory=list)
    expected: dict[str, dict[Any, dict]] = field(default_factory=dict)


def normalize_document(collection: str, raw: dict) -> dict:
    """Validate old and current records without initializing Beanie or its indexes."""
    target = TARGETS.get(collection, collection)
    values = dict(raw)
    if "_id" not in values:
        raise MigrationError(f"{collection}: missing _id")
    if target == "SummaryJob":
        kind = "manual" if collection == "ManualSummaryJob" else "auto"
        if collection in TARGETS and values.get("job_type", kind) != kind:
            raise MigrationError(
                f"{collection}: conflicting job_type for {values['_id']}"
            )
        values.setdefault("job_type", kind)
        if values["job_type"] == "manual" and collection == "ManualSummaryJob":
            destination = values.pop("channel_id", None)
            if values.get("discord_channel_id", destination) != destination:
                raise MigrationError(f"{collection}: conflicting Discord destination")
            values["discord_channel_id"] = destination
        if values.get("status") == "done":
            values["status"] = "completed"
    if target == "VideoSummary" and values.get("topics") is None:
        values["topics"] = []
    model = MODELS[target]
    instance = model.model_construct()
    try:
        model.__pydantic_validator__.validate_python(values, self_instance=instance)
    except (ValidationError, ValueError) as exc:
        raise MigrationError(f"{collection} {values['_id']}: {exc}") from exc
    # Preserve unknown fields as well as IDs; materialize defaults in the new schema.
    values.update(
        instance.model_dump(mode="python", by_alias=True, exclude={"revision_id"})
    )
    if target == "SummaryJob":
        size = len(values["delivery_chunks"])
        if not 0 <= values["delivery_chunk_index"] <= size:
            raise MigrationError(
                f"{collection} {values['_id']}: invalid delivery checkpoint"
            )
    return values


def plan_migration(snapshot: dict[str, list[dict]]) -> MigrationPlan:
    """Preflight every document and unique identity before any database writes."""
    plan = MigrationPlan(expected={name: {} for name in MODELS})
    originals = {name: {} for name in MODELS}
    for name in MODELS:
        for row in snapshot.get(name, []):
            if "_id" not in row:
                raise MigrationError(f"{name}: missing _id")
            originals[name][row["_id"]] = row
    for collection in (*MODELS, *TARGETS):
        target = TARGETS.get(collection, collection)
        for raw in snapshot.get(collection, []):
            normalized = normalize_document(collection, raw)
            identity = normalized["_id"]
            existing = plan.expected[target].get(identity)
            if existing is not None and existing != normalized:
                raise MigrationError(
                    f"{collection}: _id conflict at {identity}; target differs (possibly newer progress)"
                )
            plan.expected[target][identity] = normalized
    for target, records in plan.expected.items():
        keys = set()
        for identity, document in records.items():
            key = None
            if target == "FollowedChannel":
                key = (document["channel_id"],)
            elif target == "VideoSummary":
                key = (
                    document["source"],
                    document["video_id"],
                    document["output_language"],
                )
            elif document["job_type"] == "auto":
                key = (document["channel_id"], document["video_id"])
            if key is not None:
                if key in keys:
                    raise MigrationError(f"{target}: duplicate unique identity {key}")
                keys.add(key)
            original = originals[target].get(identity)
            if original != document:
                plan.writes.append(Write(target, document, original))
    return plan


async def ensure_indexes(database) -> None:
    """Establish new constraints before removing only obsolete uniqueness indexes."""
    for name, model in MODELS.items():
        indexes = model.Settings.indexes
        # Beanie accepts shorthand lists; PyMongo requires IndexModel objects.
        await database[name].create_indexes(
            [
                index if isinstance(index, IndexModel) else IndexModel(index)
                for index in indexes
            ]
        )
    cache_collection = database["VideoSummary"]
    await cache_collection.create_indexes([VIDEO_LANGUAGE_CACHE_INDEX])
    for name, index in (await cache_collection.index_information()).items():
        if index.get("unique") and list(index.get("key", [])) == [
            ("source", 1),
            ("video_id", 1),
        ]:
            try:
                await cache_collection.drop_index(name)
            except OperationFailure as exc:
                if exc.code != 27:
                    raise
            logger.info("Removed obsolete video cache index %s.", name)
    collection = database["SummaryJob"]
    for name, index in (await collection.index_information()).items():
        if (
            index.get("unique")
            and list(index["key"]) == [("channel_id", 1), ("video_id", 1)]
            and not index.get("partialFilterExpression")
        ):
            await collection.drop_index(name)


async def migrate(database, *, apply: bool = False) -> MigrationPlan:
    """Plan, optionally apply, and verify an offline migration. Never drop sources."""
    snapshot = {
        name: await database[name].find({}).to_list(length=None)
        for name in (*MODELS, *TARGETS)
    }
    plan = plan_migration(snapshot)
    if not apply:
        return plan
    await ensure_indexes(database)
    for write in plan.writes:
        collection = database[write.collection]
        if write.original is None:
            await collection.insert_one(write.document)
        else:
            # Match the whole original document to detect unexpected concurrent writes.
            current = await collection.find_one({"_id": write.document["_id"]})
            if current != write.original:
                raise MigrationError(
                    f"{write.collection}: concurrent change at {write.document['_id']}; stop the bot"
                )
            result = await collection.replace_one(write.original, write.document)
            if result.matched_count != 1:
                raise MigrationError(
                    "Concurrent change while migrating; stop all bot processes"
                )
    await ensure_indexes(database)
    for name, expected in plan.expected.items():
        actual = {
            row["_id"]: row
            for row in await database[name].find({}).to_list(length=None)
        }
        if actual != expected:
            raise MigrationError(f"{name}: post-migration verification failed")
        indexes = await database[name].index_information()
        for definition in MODELS[name].Settings.indexes:
            wanted = (
                definition
                if isinstance(definition, IndexModel)
                else IndexModel(definition)
            ).document
            if not any(
                list(index["key"]) == list(wanted["key"].items())
                and bool(index.get("unique")) == bool(wanted.get("unique"))
                and index.get("partialFilterExpression")
                == wanted.get("partialFilterExpression")
                for index in indexes.values()
            ):
                raise MigrationError(f"{name}: index verification failed")
    return plan


async def run(apply: bool) -> None:
    load_dotenv()
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise MigrationError("MONGODB_URI is required")
    client = AsyncMongoClient(uri, tz_aware=True)
    try:
        plan = await migrate(client[DATABASE_NAME], apply=apply)
        counts = {name: len(rows) for name, rows in plan.expected.items()}
        print(
            f"{'Applied and verified' if apply else 'Dry-run (no writes)'}: {len(plan.writes)} writes; target counts: {counts}"
        )
        print("Source collections retained. Stop all bot processes before applying.")
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes; all bot processes must be stopped",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args.apply))
    except (MigrationError, PyMongoError) as exc:
        parser.exit(1, f"Migration failed: {exc}\n")


if __name__ == "__main__":
    main()
