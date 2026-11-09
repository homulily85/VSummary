"""Persistent, effective-dated topic policies for automatic summaries."""

from __future__ import annotations

from datetime import UTC, datetime

from pymongo.errors import DuplicateKeyError

from vsummary.model.channel import AutoSummaryTopicPolicy
from vsummary.util.holodex import DEFAULT_IGNORED_TOPICS

DEFAULT_POLICY_EFFECTIVE_AT = datetime(1970, 1, 1, tzinfo=UTC)


def normalize_topic_id(topic_id: str) -> tuple[str, str]:
    """Return display and case-insensitive lookup forms of a non-blank topic ID."""
    display = topic_id.strip()
    if not display:
        raise ValueError("Topic ID cannot be empty.")
    return display, display.casefold()


async def policy_ignores_video(
    topic_id: str | None,
    published_at: datetime,
) -> bool:
    """Query the latest topic policy strictly before video publication."""
    if topic_id is None:
        return False
    if published_at.tzinfo is None:
        raise ValueError("published_at must be timezone-aware")
    _, topic_key = normalize_topic_id(topic_id)
    policy = await (
        AutoSummaryTopicPolicy.find(
            {"topic_key": topic_key, "effective_at": {"$lt": published_at}}
        )
        .sort("-effective_at")
        .first_or_none()
    )
    return policy.ignored if policy is not None else False


async def get_ignored_auto_summary_topics() -> list[str]:
    """Return topics whose latest policy is ignored, ordered by display ID."""
    topics = await AutoSummaryTopicPolicy.aggregate(
        [
            {"$sort": {"topic_key": -1, "effective_at": -1}},
            {
                "$group": {
                    "_id": "$topic_key",
                    "topic_id": {"$first": "$topic_id"},
                    "ignored": {"$first": "$ignored"},
                }
            },
            # Filter after grouping so an older ignore cannot override an allow.
            {"$match": {"ignored": True}},
            {"$sort": {"topic_id": 1}},
            {"$project": {"_id": 0, "topic_id": 1}},
        ]
    ).to_list()
    return [topic["topic_id"] for topic in topics]


async def ensure_default_auto_summary_topic_policies() -> None:
    """Seed missing built-in ignored-topic policies without overwriting user changes."""
    default_topics = {
        normalize_topic_id(topic_id)[1]: topic_id for topic_id in DEFAULT_IGNORED_TOPICS
    }
    existing = await AutoSummaryTopicPolicy.find(
        {"topic_key": {"$in": list(default_topics)}}
    ).to_list()
    existing_keys = {policy.topic_key for policy in existing}
    for topic_key, topic_id in default_topics.items():
        if topic_key in existing_keys:
            continue
        try:
            await AutoSummaryTopicPolicy(
                topic_id=topic_id,
                topic_key=topic_key,
                ignored=True,
                effective_at=DEFAULT_POLICY_EFFECTIVE_AT,
            ).insert()
        except DuplicateKeyError:
            continue


async def set_auto_summary_topic_policy(
    topic_id: str,
    *,
    ignored: bool,
    effective_at: datetime | None = None,
) -> tuple[bool, AutoSummaryTopicPolicy | None]:
    """Append a policy transition unless the topic already has the desired state."""
    display, topic_key = normalize_topic_id(topic_id)
    effective_at = effective_at or datetime.now(UTC)
    if effective_at.tzinfo is None:
        raise ValueError("effective_at must be timezone-aware")
    effective_at = effective_at.astimezone(UTC)
    current = await (
        AutoSummaryTopicPolicy.find({"topic_key": topic_key})
        .sort("-effective_at")
        .first_or_none()
    )
    if (current is None and not ignored) or (
        current is not None and current.ignored is ignored
    ):
        return False, current
    policy = AutoSummaryTopicPolicy(
        topic_id=display,
        topic_key=topic_key,
        ignored=ignored,
        effective_at=effective_at,
    )
    await policy.insert()
    return True, policy
