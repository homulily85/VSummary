"""Read and append effective-dated topic decisions for automatic summaries."""

from datetime import UTC, datetime

from pymongo.errors import DuplicateKeyError

from vsummary.model.channel import AutoSummaryTopicPolicy
from vsummary.util.holodex import DEFAULT_IGNORED_TOPICS

DEFAULT_POLICY_EFFECTIVE_AT = datetime(1970, 1, 1, tzinfo=UTC)


def normalize_topic_id(topic_id: str) -> tuple[str, str]:
    """Normalize the display ID and lookup key of a Holodex topic.

    Args:
        topic_id: User-supplied topic ID.

    Returns:
        Pair containing the trimmed display ID and its case-folded lookup key.

    Raises:
        ValueError: If the topic ID is blank.
    """
    display = topic_id.strip()
    if not display:
        raise ValueError("Topic ID cannot be empty.")
    return display, display.casefold()


async def policy_ignores_video(
    topic_id: str | None,
    published_at: datetime,
) -> bool:
    """Resolve a topic policy at the time a video was published.

    Args:
        topic_id: Holodex topic ID, or ``None`` if unknown.
        published_at: Timezone-aware video publication time when a topic is supplied.

    Returns:
        Whether the last decision strictly before publication was to ignore the topic.
            Return ``False`` when the topic or decision is absent.

    Raises:
        ValueError: If a supplied topic is blank or its publication time lacks a
            timezone.
        pymongo.errors.PyMongoError: If MongoDB cannot read the policy.
    """
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
    """List topics whose most recent stored decision is to ignore them.

    Returns:
        Display IDs sorted by display ID.

    Raises:
        pymongo.errors.PyMongoError: If MongoDB cannot aggregate the policy history.
    """
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
    """Insert epoch-dated ignore decisions for built-in topics without a history.

    Treat duplicate inserts from concurrent seeders as already applied.

    Raises:
        pymongo.errors.PyMongoError: If another MongoDB read or write error occurs.
    """
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
    """Append an allow or ignore decision when it changes the current state.

    Args:
        topic_id: Holodex topic ID to update.
        ignored: Whether to ignore the topic.
        effective_at: Timezone-aware activation time. Defaults to the current UTC time.

    Returns:
        Pair containing whether a record was inserted and the new or existing record.
            The record is ``None`` when allowing a topic with no history.

    Raises:
        ValueError: If the topic is blank or the activation time lacks a timezone.
        pymongo.errors.PyMongoError: If MongoDB cannot read or insert the policy.
    """
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
