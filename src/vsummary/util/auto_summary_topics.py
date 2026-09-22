"""Persistent, effective-dated topic policies for automatic summaries."""

from __future__ import annotations

from collections.abc import Sequence
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


def policy_ignores_video(
    policies: Sequence[AutoSummaryTopicPolicy],
    topic_id: str | None,
    published_at: datetime,
) -> bool:
    """Return the topic policy state effective strictly before video publication."""
    if topic_id is None:
        return False
    if published_at.tzinfo is None:
        raise ValueError("published_at must be timezone-aware")
    _, topic_key = normalize_topic_id(topic_id)
    effective = [
        policy
        for policy in policies
        if policy.topic_key == topic_key and policy.effective_at < published_at
    ]
    return (
        max(effective, key=lambda policy: policy.effective_at).ignored
        if effective
        else False
    )


def current_topic_policies(
    policies: Sequence[AutoSummaryTopicPolicy],
) -> dict[str, AutoSummaryTopicPolicy]:
    """Return the latest policy record for each normalized topic ID."""
    current: dict[str, AutoSummaryTopicPolicy] = {}
    for policy in policies:
        previous = current.get(policy.topic_key)
        if previous is None or policy.effective_at > previous.effective_at:
            current[policy.topic_key] = policy
    return current


async def ensure_default_auto_summary_topic_policies() -> None:
    """Seed missing built-in ignored-topic policies without overwriting user changes."""
    for topic_id in DEFAULT_IGNORED_TOPICS:
        _, topic_key = normalize_topic_id(topic_id)
        existing = await AutoSummaryTopicPolicy.find_one(
            AutoSummaryTopicPolicy.topic_key == topic_key
        )
        if existing is not None:
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


async def get_auto_summary_topic_policies() -> list[AutoSummaryTopicPolicy]:
    """Seed defaults if needed, then return the full topic-policy history."""
    await ensure_default_auto_summary_topic_policies()
    return await AutoSummaryTopicPolicy.find().to_list()


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
    policies = await get_auto_summary_topic_policies()
    current = current_topic_policies(policies).get(topic_key)
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
