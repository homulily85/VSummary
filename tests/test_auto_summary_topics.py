from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

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


def test_adding_a_topic_only_ignores_videos_published_after_the_change():
    added_at = datetime(2026, 9, 22, 12, tzinfo=UTC)
    policies = [policy("shorts", True, added_at)]

    assert not policy_ignores_video(policies, "shorts", added_at - timedelta(seconds=1))
    assert policy_ignores_video(policies, "SHORTS", added_at + timedelta(seconds=1))


def test_removing_a_default_topic_only_enables_videos_published_after_removal():
    removed_at = datetime(2026, 9, 22, 12, tzinfo=UTC)
    policies = [
        policy("shorts", True, datetime(1970, 1, 1, tzinfo=UTC)),
        policy("shorts", False, removed_at),
    ]

    assert policy_ignores_video(policies, "shorts", removed_at - timedelta(seconds=1))
    assert not policy_ignores_video(
        policies, "shorts", removed_at + timedelta(seconds=1)
    )


def test_topic_policy_history_uses_the_state_effective_when_the_video_was_published():
    initial = datetime(1970, 1, 1, tzinfo=UTC)
    enabled_at = datetime(2026, 9, 20, tzinfo=UTC)
    ignored_again_at = datetime(2026, 9, 21, tzinfo=UTC)
    policies = [
        policy("shorts", True, initial),
        policy("shorts", False, enabled_at),
        policy("shorts", True, ignored_again_at),
    ]

    assert policy_ignores_video(policies, "shorts", enabled_at - timedelta(seconds=1))
    assert not policy_ignores_video(
        policies, "shorts", enabled_at + timedelta(seconds=1)
    )
    assert policy_ignores_video(
        policies, "shorts", ignored_again_at + timedelta(seconds=1)
    )


def test_missing_topic_id_is_never_ignored():
    assert not policy_ignores_video([], None, datetime.now(UTC))


async def test_allowing_an_unconfigured_topic_is_a_noop(monkeypatch):
    """Removing an already-allowed topic must not create policy history."""
    get_policies = AsyncMock(return_value=[])
    monkeypatch.setattr(
        auto_summary_topics, "get_auto_summary_topic_policies", get_policies
    )

    changed, result = await auto_summary_topics.set_auto_summary_topic_policy(
        "unlisted", ignored=False
    )

    assert not changed
    assert result is None
