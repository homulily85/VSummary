"""Structured retrieval and evidence checks for the internal YouTube wiki."""

import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime, timedelta

from beanie import Document

from vsummary.model.wiki import (
    Mention,
    Validity,
    WikiAlias,
    WikiEntity,
    WikiEvidence,
    WikiFact,
)
from vsummary.util.video_metadata import VideoMetadata


def normalize_name(value: str) -> str:
    """Normalize a name for comparison and lookup.

    Args:
        value: Name or text to compare.

    Returns:
        Text with Unicode compatibility variants, case, and whitespace normalized.
    """
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def digest(*values: str) -> str:
    """Derive a stable hash from an ordered sequence of strings.

    Args:
        *values: Components of the key or content fingerprint.

    Returns:
        Hexadecimal SHA-256 digest of the JSON-encoded sequence.
    """
    encoded = json.dumps(values, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def correction_table(mentions: list[Mention]) -> str:
    """Serialize verified spelling changes for the summary notebook.

    Args:
        mentions: Transcript mentions after wiki lookup and research.

    Returns:
        JSON rows with the original name, its context, and the verified spelling,
        or an empty string when there are no corrections.
    """
    rows = [
        {"transcript": item.original, "context": item.quote, "name": item.canonical}
        for item in mentions
        if item.accepted and item.original != item.canonical
    ]
    return json.dumps(rows, ensure_ascii=False) if rows else ""


async def insert_once(model: type[Document], document_id: str, **fields) -> None:
    # MongoDB's unique _id makes overlapping retries safe.
    """Insert a wiki record only if its deterministic ID is absent.

    Args:
        model: Beanie document type that owns the target collection.
        document_id: Stable MongoDB ID for the record.
        **fields: Values to write on insertion.

    Raises:
        pymongo.errors.PyMongoError: If the upsert fails.
    """
    await model.get_pymongo_collection().update_one(
        {"_id": document_id}, {"$setOnInsert": fields}, upsert=True
    )


def mention_date(mention: Mention, video_date: date | None) -> date | None:
    """Select the date used to check historical names.

    Args:
        mention: Transcript mention with an optional explicit date.
        video_date: Date of the recording or publication, if known.

    Returns:
        The explicit date when present, or the video date when the quote
        contains no year that conflicts with it. Return None if the date
        cannot be determined.
    """
    if mention.at:
        return mention.at
    years = set(re.findall(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])", mention.quote))
    if years and (video_date is None or years != {str(video_date.year)}):
        return None
    return video_date


async def resolve_alias(
    mention: Mention,
    channel_id: str | None,
    video_date: date | None,
    *,
    profiles: set[str] | None = None,
) -> Mention:
    """Resolve a transcript name against the dated wiki.

    A matching alias must have channel scope or a link to an authoritative
    profile. Conflicting or ambiguous names remain unresolved.

    Args:
        mention: Transcript mention to resolve.
        channel_id: YouTube channel ID used to scope aliases, when known.
        video_date: Date used when the mention has no explicit date.
        profiles: Normalized authoritative profile URLs linked by the video.

    Returns:
        The mention with candidate IDs, or a resolved copy with a canonical
        name and supporting evidence.

    Raises:
        pymongo.errors.PyMongoError: If a wiki lookup fails.
    """
    aliases = (
        await WikiAlias.find(
            {
                "key": normalize_name(mention.original),
                "kind": mention.kind,
                "status": {"$in": ["verified", "conflicted"]},
                "channel_id": {"$in": [None, channel_id]},
                "speaker_id": None,
            }
        )
        .limit(21)
        .to_list()
    )
    if len(aliases) > 20:
        return mention
    at = mention_date(mention, video_date)
    applicable = [alias for alias in aliases if alias.validity.covers(at)]
    candidates = {alias.entity_id for alias in applicable}
    mention.candidate_ids = sorted(candidates)
    trusted = {
        alias.entity_id
        for alias in applicable
        if channel_id and alias.channel_id == channel_id
    }
    if candidates and profiles:
        linked = await WikiEntity.find(
            {
                "_id": {"$in": list(candidates)},
                "external_key": {"$in": [f"{mention.kind}:{url}" for url in profiles]},
            }
        ).to_list()
        trusted.update(entity.id for entity in linked)
    if len(trusted) != 1:
        return mention
    entity_id = next(iter(trusted))
    applicable = [alias for alias in applicable if alias.entity_id == entity_id]
    if any(alias.status == "conflicted" for alias in applicable):
        return mention

    # Read the dated canonical name, not the entity's latest display name.
    names = (
        await WikiFact.find(
            {
                "subject_id": entity_id,
                "relation": "canonical_name",
                "status": {"$in": ["verified", "conflicted"]},
            }
        )
        .limit(21)
        .to_list()
    )
    if len(names) > 20:
        return mention
    facts = [fact for fact in names if fact.validity.covers(at)]
    if len({fact.value for fact in facts}) == 1 and all(
        fact.status == "verified" for fact in facts
    ):
        evidence = sorted(
            {item for row in [*facts, *applicable] for item in row.evidence_ids}
        )
        return mention.model_copy(
            update={
                "entity_id": entity_id,
                "canonical": facts[0].value,
                "evidence_ids": evidence,
            }
        )
    return mention


async def learn_name(
    mention: Mention,
    *,
    canonical: str,
    url: str,
    excerpt: str,
    content: str,
    channel_id: str | None,
    video_id: str,
    validity: Validity,
    attested_alias: bool,
) -> Mention:
    """Persist evidence for a verified name and associate it with the mention.

    Keep overlapping, incompatible names as conflicting facts. Only store a
    transcript spelling as a reusable alias when the source attests it.

    Args:
        mention: Transcript mention being verified.
        canonical: Name confirmed by the source.
        url: Normalized URL of the authoritative profile.
        excerpt: Verbatim passage supporting the name.
        content: Source text used to fingerprint the evidence.
        channel_id: Scope for learned aliases. Without a channel ID, store
            evidence for this video without adding a shared name or alias.
        video_id: Video that prompted the lookup.
        validity: Period supported by the source.
        attested_alias: Whether the source explicitly supports the original
            spelling as an alias.

    Returns:
        A copy with the accepted name and evidence, or the unresolved
        mention when the proposed name conflicts with a stored fact.

    Raises:
        pymongo.errors.PyMongoError: If a wiki lookup or write fails.
    """
    now = datetime.now(UTC)
    # Identify the entity by an authoritative profile URL plus type. An ambiguous
    # shared page must not be used as an identity key.
    external_key = f"{mention.kind}:{url}"
    entity_id = digest(external_key)
    content_hash = digest(content)
    evidence_id = digest(url, content_hash, excerpt)
    await insert_once(
        WikiEvidence,
        evidence_id,
        url=url,
        excerpt=excerpt,
        content_hash=content_hash,
        observed_at=now,
        video_id=video_id,
    )
    if not channel_id:
        return mention.model_copy(
            update={"canonical": canonical, "evidence_ids": [evidence_id]}
        )
    await insert_once(
        WikiEntity,
        entity_id,
        kind=mention.kind,
        name=canonical,
        external_key=external_key,
        recorded_at=now,
    )
    at = validity.start
    prior = (
        await WikiFact.find(
            {
                "subject_id": entity_id,
                "relation": "canonical_name",
                "value": {"$ne": canonical},
            }
        )
        .limit(21)
        .to_list()
    )
    conflicted = len(prior) > 20 or any(
        fact.validity.start is not None
        and (validity.end is None or fact.validity.start < validity.end)
        and (fact.validity.end is None or at is None or at < fact.validity.end)
        for fact in prior
    )
    # Include the interval and evidence in the ID so new evidence is additive.
    period = validity.model_dump(mode="json")
    version = validity.model_dump_json()
    await insert_once(
        WikiFact,
        digest(entity_id, "canonical_name", canonical, version, evidence_id),
        subject_id=entity_id,
        relation="canonical_name",
        value=canonical,
        validity=period,
        status="conflicted" if conflicted else "verified",
        evidence_ids=[evidence_id],
        recorded_at=now,
    )
    if conflicted:
        return mention
    aliases = [(canonical, None)]
    if attested_alias:
        aliases.append((mention.original, channel_id))
    for name, scope in aliases:
        key = normalize_name(name)
        await insert_once(
            WikiAlias,
            digest(entity_id, name, scope or "", version, evidence_id),
            entity_id=entity_id,
            kind=mention.kind,
            text=name,
            key=key,
            channel_id=scope,
            validity=period,
            status="verified",
            evidence_ids=[evidence_id],
            recorded_at=now,
        )
    return mention.model_copy(
        update={
            "entity_id": entity_id,
            "canonical": canonical,
            "evidence_ids": [evidence_id],
        }
    )


async def record_mentions(
    metadata: VideoMetadata,
    video_id: str,
    transcript_hash: str,
    mentions: list[Mention],
) -> None:
    """Record which verified entities a video mentions on its channel.

    Store the transcript excerpt as evidence for the mention itself.

    Args:
        metadata: Video metadata with the publisher's YouTube channel ID.
        video_id: Video that contains the mentions.
        transcript_hash: Fingerprint of the transcript used for verification.
        mentions: Mentions to inspect for accepted entity links.

    Raises:
        pymongo.errors.PyMongoError: If a wiki write fails.
    """
    channel_id = metadata.channel_id
    if not channel_id:
        return
    observed = [item for item in mentions if item.accepted and item.entity_id]
    if not observed:
        return
    now = datetime.now(UTC)
    external_key = f"youtube_channel:{channel_id}"
    channel_entity = digest(external_key)
    await insert_once(
        WikiEntity,
        channel_entity,
        kind="channel",
        name=metadata.channel_name or channel_id,
        external_key=external_key,
        recorded_at=now,
    )
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    for item in observed:
        evidence_id = digest(video_url, transcript_hash, item.quote)
        await insert_once(
            WikiEvidence,
            evidence_id,
            url=video_url,
            excerpt=item.quote,
            content_hash=transcript_hash,
            observed_at=now,
            video_id=video_id,
        )
        at = metadata.content_date
        period = Validity(start=at, end=at + timedelta(days=1) if at else None)
        await insert_once(
            WikiFact,
            digest(item.entity_id, "mentioned_by_channel", channel_entity, evidence_id),
            subject_id=item.entity_id,
            relation="mentioned_by_channel",
            value=channel_entity,
            validity=period.model_dump(mode="json"),
            status="verified",
            evidence_ids=[evidence_id, *item.evidence_ids],
            recorded_at=now,
        )
