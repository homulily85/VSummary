"""Run against a disposable MongoDB: MONGODB_TEST_URI=mongodb://127.0.0.1:27028.

Each test uses and removes its own randomly named test database.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

from vsummary.cogs.autosummary.autosummary import Autosummary
from vsummary.cogs.manualsummary.manualsummary import ManualSummary
from vsummary.migrations.v1_6 import MigrationError, migrate, normalize_document
from vsummary.model.channel import SummaryJob


@pytest.fixture
async def database():
    uri = os.environ.get("MONGODB_TEST_URI")
    if not uri:
        pytest.skip("Set MONGODB_TEST_URI to a disposable MongoDB server")
    client = AsyncMongoClient(uri, tz_aware=True)
    database = client["vsummary_migration_test_" + uuid.uuid4().hex]
    try:
        yield database
    finally:
        await client.drop_database(database.name)
        await client.close()


def old_auto(**values):
    now = datetime.now(UTC).replace(microsecond=0)
    return dict(
        _id=ObjectId(),
        video_id="video",
        channel_id="UC1",
        channel_name="Name",
        title="Title",
        available_at=now,
        next_attempt_at=now,
        **values,
    )


def old_manual(**values):
    return dict(
        _id=ObjectId(),
        source="youtube",
        video_id="video",
        channel_id=123,
        requester_id=456,
        operation="topics",
        next_attempt_at=datetime.now(UTC).replace(microsecond=0),
        **values,
    )


async def snapshot(database):
    return {
        name: await database[name].find({}).to_list(None)
        for name in await database.list_collection_names()
    }


async def test_dry_run_apply_resume_indexes_and_sources(database):
    auto = old_auto(status="done")
    manual = old_manual(
        status="delivering",
        delivery_chunks=["first", "second"],
        delivery_chunk_index=1,
        claimed_by="old-worker",
        lease_expires_at=datetime.now(UTC).replace(microsecond=0),
    )
    await database.PendingVideo.insert_one(auto)
    await database.ManualSummaryJob.insert_one(manual)
    await database.Channel.insert_one(
        {
            "_id": ObjectId(),
            "channel_id": "UC1",
            "name": "Name",
            "added_at": auto["available_at"],
        }
    )
    await database.Video.insert_one(
        {"_id": ObjectId(), "source": "youtube", "video_id": "video", "topics": None}
    )
    # Existing renamed collections and old constraints must also be supported.
    await database.SummaryJob.insert_one(auto)
    await database.SummaryJob.create_index(
        [("channel_id", 1), ("video_id", 1)], unique=True
    )
    await database.SummaryJob.create_index("title", name="unrelated_title")
    await database.VideoSummary.create_index(
        [("source", 1), ("video_id", 1)], unique=True
    )
    before = await snapshot(database)
    await migrate(database)
    assert await snapshot(database) == before
    await migrate(database, apply=True)
    migrated = await database.SummaryJob.find_one({"_id": manual["_id"]})
    assert migrated["delivery_chunk_index"] == 1
    assert migrated["claimed_by"] == "old-worker"
    assert migrated["discord_channel_id"] == 123
    assert await database.PendingVideo.find_one({}) == auto
    assert await database.ManualSummaryJob.find_one({}) == manual
    indexes = await database.SummaryJob.index_information()
    assert "unrelated_title" in indexes
    assert "channel_id_1_video_id_1" not in indexes
    assert indexes["auto_channel_video_unique"]["partialFilterExpression"] == {
        "job_type": "auto"
    }
    assert "source_1_video_id_1" not in await database.VideoSummary.index_information()
    assert (await migrate(database, apply=True)).writes == []
    # Actual MongoDB uniqueness allows repeat manual requests but rejects duplicate auto.
    another = normalize_document("ManualSummaryJob", old_manual())
    await database.SummaryJob.insert_one(another)
    duplicate = normalize_document("PendingVideo", old_auto())
    with pytest.raises(DuplicateKeyError):
        await database.SummaryJob.insert_one(duplicate)


async def test_conflict_and_invalid_schema_do_not_write(database):
    auto = old_auto()
    await database.PendingVideo.insert_one(auto)
    target = normalize_document("PendingVideo", auto)
    target["retry_count"] = 8
    await database.SummaryJob.insert_one(target)
    before = await snapshot(database)
    with pytest.raises(MigrationError, match="conflict"):
        await migrate(database, apply=True)
    assert await snapshot(database) == before


async def test_workers_claim_only_their_job_type(database, monkeypatch):
    now = datetime.now(UTC).replace(microsecond=0)
    auto = normalize_document("PendingVideo", old_auto())
    manual = normalize_document("ManualSummaryJob", old_manual())
    manual.update(
        status="delivering",
        lease_expires_at=now - timedelta(minutes=1),
        delivery_chunks=["saved"],
    )
    await database.SummaryJob.insert_many([auto, manual])
    monkeypatch.setattr(
        SummaryJob,
        "get_pymongo_collection",
        classmethod(lambda cls: database.SummaryJob),
    )
    auto_worker = Autosummary.__new__(Autosummary)
    auto_worker.settings = None
    auto_worker.summary_service = None
    auto_worker.notebook = None
    auto_worker.worker_id = "auto-worker"
    manual_worker = ManualSummary.__new__(ManualSummary)
    manual_worker.settings = None
    manual_worker.worker_id = "manual-worker"
    claimed_auto = await auto_worker._claim_due_jobs()
    assert [job.id for job in claimed_auto] == [auto["_id"]]
    claimed_manual = await manual_worker._claim_due_jobs()
    assert [job.id for job in claimed_manual] == [manual["_id"]]
    assert claimed_manual[0].delivery_chunks == ["saved"]
    await database.SummaryJob.update_one(
        {"_id": auto["_id"]}, {"$set": {"claimed_by": "new-worker"}}
    )
    assert not await auto_worker._save_item(claimed_auto[0])
    await database.SummaryJob.update_one(
        {"_id": manual["_id"]}, {"$set": {"claimed_by": "new-worker"}}
    )
    assert not await manual_worker._save(claimed_manual[0])


async def test_real_interruption_can_be_resumed(database, monkeypatch):
    from pymongo.asynchronous.collection import AsyncCollection

    auto = old_auto()
    manual = old_manual()
    await database.PendingVideo.insert_one(auto)
    await database.ManualSummaryJob.insert_one(manual)
    original = AsyncCollection.insert_one
    calls = 0

    async def interrupt(collection, document, *args, **kwargs):
        nonlocal calls
        if collection.full_name == database.SummaryJob.full_name:
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interruption")
        return await original(collection, document, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncCollection, "insert_one", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            await migrate(database, apply=True)
    assert await database.SummaryJob.count_documents({}) == 1
    await migrate(database, apply=True)
    assert await database.SummaryJob.count_documents({}) == 2
    assert (await migrate(database, apply=True)).writes == []


async def test_ignored_topics_use_latest_policy_per_topic(database, monkeypatch):
    from vsummary.util import auto_summary_topics

    collection = database.AutoSummaryTopicPolicy
    initial = datetime(2026, 9, 20, tzinfo=UTC)
    later = initial + timedelta(days=1)
    await collection.insert_many(
        [
            {
                "topic_key": key,
                "topic_id": display,
                "ignored": ignored,
                "effective_at": at,
            }
            for key, display, ignored, at in [
                ("shorts", "shorts", False, later),
                ("shorts", "SHORTS", True, initial),
                ("music", "Music", True, later),
                ("music", "music", False, initial),
                ("art", "Art", True, initial),
            ]
        ]
    )

    class PolicyCollection:
        @staticmethod
        def aggregate(pipeline):
            class Query:
                async def to_list(self):
                    return await (await collection.aggregate(pipeline)).to_list(None)

            return Query()

    monkeypatch.setattr(auto_summary_topics, "AutoSummaryTopicPolicy", PolicyCollection)

    assert await auto_summary_topics.get_ignored_auto_summary_topics() == [
        "Art",
        "Music",
    ]


async def test_video_topic_policy_uses_latest_transition_before_publication(database):
    from beanie import init_beanie

    from vsummary.model.channel import AutoSummaryTopicPolicy
    from vsummary.util.auto_summary_topics import policy_ignores_video

    await init_beanie(database=database, document_models=[AutoSummaryTopicPolicy])
    initial = datetime(2026, 9, 20, tzinfo=UTC)
    allowed = initial + timedelta(days=1)
    ignored_again = allowed + timedelta(days=1)
    # Insert out of order to ensure the query selects by effective_at.
    for topic, ignored, at in [
        ("shorts", True, ignored_again),
        ("shorts", True, initial),
        ("shorts", False, allowed),
        ("music", True, initial),
    ]:
        await AutoSummaryTopicPolicy(
            topic_id=topic, topic_key=topic, ignored=ignored, effective_at=at
        ).insert()

    for published_at, expected in [
        (initial - timedelta(seconds=1), False),
        (initial, False),
        (initial + timedelta(seconds=1), True),
        (allowed, True),
        (allowed + timedelta(seconds=1), False),
        (ignored_again, False),
        (ignored_again + timedelta(seconds=1), True),
    ]:
        assert await policy_ignores_video(" SHORTS ", published_at) is expected
    assert not await policy_ignores_video("unknown", ignored_again)
