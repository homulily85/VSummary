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
