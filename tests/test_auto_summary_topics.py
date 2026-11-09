from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from vsummary.model.channel import AutoSummaryTopicPolicy
from vsummary.util import auto_summary_topics
from vsummary.util.auto_summary_topics import (
    DEFAULT_IGNORED_TOPICS,
    policy_ignores_video,
)


def policy(topic_id: str, ignored: bool, effective_at: datetime):
    return AutoSummaryTopicPolicy.model_construct(
        topic_id=topic_id,
        topic_key=topic_id.casefold(),
        ignored=ignored,
        effective_at=effective_at,
    )


def test_default_topic_ids_remain_the_initial_auto_summary_blacklist():
    assert DEFAULT_IGNORED_TOPICS == {
        "Original_Song",
        "membersonly",
        "shorts",
        "Music_Cover",
    }


@pytest.mark.parametrize("ignored", [None, False, True])
async def test_policy_query_uses_normalized_topic_and_strict_publication_cutoff(
    monkeypatch, ignored
):
    published_at = datetime(2026, 9, 22, 12, tzinfo=UTC)
    model = MagicMock()
    model.find.return_value.sort.return_value.first_or_none = AsyncMock(
        return_value=None
        if ignored is None
        else policy("shorts", ignored, published_at)
    )
    monkeypatch.setattr(auto_summary_topics, "AutoSummaryTopicPolicy", model)

    assert await policy_ignores_video(" SHORTS ", published_at) is bool(ignored)
    model.find.assert_called_once_with(
        {"topic_key": "shorts", "effective_at": {"$lt": published_at}}
    )
    model.find.return_value.sort.assert_called_once_with("-effective_at")


async def test_missing_topic_id_is_never_ignored(monkeypatch):
    model = MagicMock()
    monkeypatch.setattr(auto_summary_topics, "AutoSummaryTopicPolicy", model)
    assert not await policy_ignores_video(None, datetime.now(UTC))
    model.find.assert_not_called()


async def test_policy_query_rejects_naive_publication_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        await policy_ignores_video(
            "shorts", datetime(2026, 9, 22, tzinfo=UTC).replace(tzinfo=None)
        )


async def test_allowing_an_unconfigured_topic_is_a_noop(monkeypatch):
    """Removing an already-allowed topic must not create policy history."""
    model = MagicMock()
    model.find.return_value.sort.return_value.first_or_none = AsyncMock(
        return_value=None
    )
    monkeypatch.setattr(auto_summary_topics, "AutoSummaryTopicPolicy", model)

    changed, result = await auto_summary_topics.set_auto_summary_topic_policy(
        "unlisted", ignored=False
    )

    assert not changed
    assert result is None
    model.find.assert_called_once_with({"topic_key": "unlisted"})
    model.find.return_value.sort.assert_called_once_with("-effective_at")
    model.assert_not_called()


async def test_seed_reads_existing_topics_once_and_preserves_user_changes(monkeypatch):
    from unittest.mock import MagicMock

    from pymongo.errors import DuplicateKeyError

    existing = policy("shorts", False, datetime.now(UTC))
    model = MagicMock()
    model.find.return_value.to_list = AsyncMock(return_value=[existing])
    model.return_value.insert = AsyncMock(
        side_effect=[DuplicateKeyError("concurrent seed"), None, None]
    )
    monkeypatch.setattr(auto_summary_topics, "AutoSummaryTopicPolicy", model)

    await auto_summary_topics.ensure_default_auto_summary_topic_policies()

    model.find.assert_called_once()
    assert set(model.find.call_args.args[0]["topic_key"]["$in"]) == {
        topic.casefold() for topic in DEFAULT_IGNORED_TOPICS
    }
    assert {call.kwargs["topic_key"] for call in model.call_args_list} == {
        topic.casefold() for topic in DEFAULT_IGNORED_TOPICS
    } - {"shorts"}
    assert model.return_value.insert.await_count == 3
