"""Run against a disposable MongoDB: MONGODB_TEST_URI=mongodb://127.0.0.1:27028.

Each test uses and removes its own randomly named test database.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

from vsummary.cogs.autosummary.autosummary import Autosummary
from vsummary.cogs.manualsummary.manualsummary import ManualSummary
from vsummary.model.channel import AutoSummaryTopicPolicy, JobStatus, SummaryJob
from vsummary.util.auto_summary_topics import (
    get_ignored_auto_summary_topics,
    policy_ignores_video,
)


@pytest.fixture
async def database():
    uri = os.environ.get("MONGODB_TEST_URI")
    if not uri:
        pytest.skip("Set MONGODB_TEST_URI to a disposable MongoDB server")
    client = AsyncMongoClient(uri, tz_aware=True)
    database = client["vsummary_test_" + uuid.uuid4().hex]
    try:
        await init_beanie(
            database=database, document_models=[SummaryJob, AutoSummaryTopicPolicy]
        )
        yield database
    finally:
        try:
            await client.drop_database(database.name)
        finally:
            await client.close()


def auto_job():
    now = datetime.now(UTC).replace(microsecond=0)
    return SummaryJob(
        job_type="auto",
        video_id="video",
        channel_id="UC1",
        channel_name="Name",
        title="Title",
        available_at=now,
        next_attempt_at=now,
    )


def manual_job(**values):
    return SummaryJob(
        job_type="manual",
        video_id="video",
        discord_channel_id=123,
        requester_id=456,
        operation="topics",
        next_attempt_at=datetime.now(UTC).replace(microsecond=0),
        **values,
    )


async def test_summary_job_uniqueness_applies_only_to_auto_jobs(database):
    await auto_job().insert()
    await manual_job().insert()
    await manual_job().insert()
    with pytest.raises(DuplicateKeyError):
        await auto_job().insert()


async def test_workers_claim_only_their_job_type(database):
    now = datetime.now(UTC).replace(microsecond=0)
    auto = await auto_job().insert()
    manual = await manual_job(
        status=JobStatus.DELIVERING,
        lease_expires_at=now - timedelta(minutes=1),
        delivery_chunks=["saved"],
    ).insert()
    auto_worker = Autosummary.__new__(Autosummary)
    auto_worker.settings = None
    auto_worker.summary_service = None
    auto_worker.notebook = None
    auto_worker.worker_id = "auto-worker"
    manual_worker = ManualSummary.__new__(ManualSummary)
    manual_worker.settings = None
    manual_worker.worker_id = "manual-worker"
    claimed_auto = await auto_worker._claim_due_jobs()
    assert [job.id for job in claimed_auto] == [auto.id]
    claimed_manual = await manual_worker._claim_due_jobs()
    assert [job.id for job in claimed_manual] == [manual.id]
    assert claimed_manual[0].delivery_chunks == ["saved"]
    await database.SummaryJob.update_one(
        {"_id": auto.id}, {"$set": {"claimed_by": "new-worker"}}
    )
    assert not await auto_worker._save_item(claimed_auto[0])
    await database.SummaryJob.update_one(
        {"_id": manual.id}, {"$set": {"claimed_by": "new-worker"}}
    )
    assert not await manual_worker._save(claimed_manual[0])


async def test_ignored_topics_use_latest_policy_per_topic(database):
    initial = datetime(2026, 9, 20, tzinfo=UTC)
    later = initial + timedelta(days=1)
    await AutoSummaryTopicPolicy.insert_many(
        [
            AutoSummaryTopicPolicy(
                topic_key=key, topic_id=display, ignored=ignored, effective_at=at
            )
            for key, display, ignored, at in [
                ("shorts", "shorts", False, later),
                ("shorts", "SHORTS", True, initial),
                ("music", "Music", True, later),
                ("music", "music", False, initial),
                ("art", "Art", True, initial),
            ]
        ]
    )

    assert await get_ignored_auto_summary_topics() == ["Art", "Music"]


async def test_video_topic_policy_uses_latest_transition_before_publication(database):
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
