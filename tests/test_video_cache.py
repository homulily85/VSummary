from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import OperationFailure

from vsummary.model.video import VideoSummary
from vsummary.util.video import VideoRef


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "languages", [[], ["english"], ["english", "japanese", None, "untagged"]]
)
async def test_delete_cached_summary_removes_all_languages_for_exact_video(
    monkeypatch, languages
):
    from vsummary.util.video_cache import delete_cached_summary

    ref = VideoRef(source="youtube", video_id="video")
    records = []
    for language in languages:
        record = {
            "source": "youtube",
            "video_id": "video",
            "topics": [{"name": "Topic", "detail": "Details"}],
        }
        if language != "untagged":
            record["output_language"] = language
        records.append(record)
    unrelated = [
        {"source": "youtube", "video_id": "other", "output_language": "english"},
        {"source": "twitch", "video_id": "video", "output_language": "english"},
    ]
    records.extend(unrelated)

    async def delete_many(query):
        before = len(records)
        records[:] = [
            record
            for record in records
            if not all(record.get(key) == value for key, value in query.items())
        ]
        return SimpleNamespace(deleted_count=before - len(records))

    collection = SimpleNamespace(delete_many=AsyncMock(side_effect=delete_many))
    monkeypatch.setattr(VideoSummary, "get_pymongo_collection", lambda: collection)

    assert await delete_cached_summary(ref) == len(languages)
    assert records == unrelated
    collection.delete_many.assert_awaited_once_with(
        {"source": "youtube", "video_id": "video"}
    )


@pytest.mark.asyncio
async def test_delete_cached_summary_propagates_database_failures(monkeypatch):
    from vsummary.util.video_cache import delete_cached_summary

    collection = SimpleNamespace(
        delete_many=AsyncMock(side_effect=OperationFailure("delete failed"))
    )
    monkeypatch.setattr(VideoSummary, "get_pymongo_collection", lambda: collection)
    with pytest.raises(OperationFailure, match="delete failed"):
        await delete_cached_summary(VideoRef(source="youtube", video_id="video"))


class FakeIndexCollection:
    def __init__(self, *, legacy=True):
        self.indexes = {
            "_id_": {"key": [("_id", 1)], "unique": True},
            "custom_lookup": {"key": [("video_id", 1)]},
            "nonunique_video_lookup": {"key": [("source", 1), ("video_id", 1)]},
        }
        if legacy:
            self.indexes["source_1_video_id_1"] = {
                "key": [("source", 1), ("video_id", 1)],
                "unique": True,
            }
        self.events = []

    async def create_indexes(self, indexes):
        self.events.append("create")
        for index in indexes:
            definition = dict(index.document)
            definition["key"] = list(definition["key"].items())
            self.indexes[definition["name"]] = definition

    async def index_information(self):
        self.events.append("inspect")
        return dict(self.indexes)

    async def drop_index(self, name):
        self.events.append("drop")
        assert any(
            index["key"] == [("source", 1), ("video_id", 1), ("output_language", 1)]
            and index.get("unique")
            for index in self.indexes.values()
        )
        del self.indexes[name]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_language_cache_index_migration_is_safe_and_idempotent(legacy):
    from vsummary.util.video_cache import migrate_video_cache_indexes

    collection = FakeIndexCollection(legacy=legacy)
    await migrate_video_cache_indexes(collection)
    await migrate_video_cache_indexes(collection)

    assert "source_1_video_id_1" not in collection.indexes
    assert set(collection.indexes) >= {
        "_id_",
        "custom_lookup",
        "nonunique_video_lookup",
    }
    indexes = list(collection.indexes.values())
    assert sum(index.get("unique", False) for index in indexes) == 2
    assert collection.events == (
        ["create", "inspect", "drop", "create", "inspect"]
        if legacy
        else ["create", "inspect", "create", "inspect"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["create_indexes", "index_information", "drop_index"])
async def test_language_cache_index_migration_propagates_failures(stage):
    from vsummary.util.video_cache import migrate_video_cache_indexes

    collection = SimpleNamespace(
        create_indexes=AsyncMock(),
        index_information=AsyncMock(
            return_value={
                "source_1_video_id_1": {
                    "key": [("source", 1), ("video_id", 1)],
                    "unique": True,
                }
            }
        ),
        drop_index=AsyncMock(),
    )
    getattr(collection, stage).side_effect = OperationFailure(
        "migration failed", code=13
    )
    with pytest.raises(OperationFailure, match="migration failed"):
        await migrate_video_cache_indexes(collection)
    if stage != "drop_index":
        collection.drop_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_language_cache_migration_accepts_concurrent_legacy_index_removal():
    from vsummary.util.video_cache import migrate_video_cache_indexes

    collection = SimpleNamespace(
        create_indexes=AsyncMock(),
        index_information=AsyncMock(
            return_value={
                "source_1_video_id_1": {
                    "key": [("source", 1), ("video_id", 1)],
                    "unique": True,
                }
            }
        ),
        drop_index=AsyncMock(side_effect=OperationFailure("index not found", code=27)),
    )
    await migrate_video_cache_indexes(collection)
    collection.drop_index.assert_awaited_once_with("source_1_video_id_1")
