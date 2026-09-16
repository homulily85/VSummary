import json
import logging
from collections.abc import Sequence

import httpx
from notebooklm import NotebookLMClient, SourceAddError
from notebooklm.types import Notebook

from vsummary.logging import log_event
from vsummary.model.video import Topic, VideoSummary
from vsummary.util.video import VideoRef, build_video_url

logger = logging.getLogger(__name__)
Video = VideoSummary


class SummaryServiceError(Exception):
    """Base error raised by the NotebookLM summary service."""


class TransientSummaryError(SummaryServiceError):
    """Raised when a summary can be retried safely."""


class PermanentSummaryError(SummaryServiceError):
    """Raised when NotebookLM returned an unusable result."""


class InvalidSummaryResponse(PermanentSummaryError):
    """Raised when NotebookLM's topic JSON cannot be shown safely in Discord."""


class NotebookLMSummaryService:
    """NotebookLM-backed summary generation with durable cache reuse."""

    def __init__(self, client: NotebookLMClient):
        self.client = client

    async def close(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()

    async def summarize(self, ref: VideoRef) -> list[Topic]:
        try:
            details = await get_topic_details_all(self.client, ref)
        except SourceAddError as exc:
            log_event(
                logger,
                logging.WARNING,
                "NotebookLM could not add source for %s.",
                ref.video_id,
                source=ref.source,
                video_id=ref.video_id,
                error_type=type(exc).__name__,
            )
            raise TransientSummaryError(str(exc)) from exc
        except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
            log_event(
                logger,
                logging.WARNING,
                "NotebookLM request failed for %s.",
                ref.video_id,
                source=ref.source,
                video_id=ref.video_id,
                error_type=type(exc).__name__,
            )
            raise TransientSummaryError(str(exc)) from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            log_event(
                logger,
                logging.ERROR,
                "NotebookLM returned an invalid summary for %s.",
                ref.video_id,
                source=ref.source,
                video_id=ref.video_id,
                error_type=type(exc).__name__,
            )
            raise PermanentSummaryError(str(exc)) from exc
        return [
            Topic(name=item["topic_name"], detail=item["detail"]) for item in details
        ]


def _topic_boundaries(topics: list[Topic], topic_index: int) -> dict[str, str]:
    """Name the topics adjacent to ``topic_index`` for prompt context."""
    return {
        "prev": topics[topic_index - 1].name
        if topic_index > 0
        else "the start of the video",
        "next": (
            topics[topic_index + 1].name
            if topic_index + 1 < len(topics)
            else "the end of the video"
        ),
    }


def _validated_topics(topics: Sequence[Topic]) -> list[Topic]:
    """Validate names used both in persisted data and Discord select labels."""
    if not topics:
        raise InvalidSummaryResponse("NotebookLM returned an empty topic list")

    validated: list[Topic] = []
    for topic in topics:
        name = topic.name.strip() if isinstance(topic.name, str) else ""
        if not name:
            raise InvalidSummaryResponse("NotebookLM returned a blank topic name")
        if len(name) > 100:
            raise InvalidSummaryResponse(
                "NotebookLM returned a topic name longer than Discord allows"
            )
        topic.name = name
        validated.append(topic)
    return validated


def _parse_topics(answer: str) -> list[Topic]:
    try:
        payload = json.loads(answer)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidSummaryResponse(
            "NotebookLM did not return JSON topic data"
        ) from exc

    if not isinstance(payload, list):
        raise InvalidSummaryResponse("NotebookLM topic data must be a list")

    topics: list[Topic] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise InvalidSummaryResponse("NotebookLM topic data is missing a name")
        topics.append(Topic(name=item["name"]))
    return _validated_topics(topics)


async def _create_notebook_with_source(
    client: NotebookLMClient, ref: VideoRef
) -> "Notebook":
    """Create a temporary notebook with the video source attached.

    The caller is responsible for deleting the returned notebook when it is
    no longer needed. If the source cannot be added, the notebook is deleted
    here before the error is re-raised.
    """
    video_link = build_video_url(ref)
    notebook = await client.notebooks.create(video_link)
    try:
        await client.sources.add_url(notebook.id, video_link)
    except Exception:
        await _safe_delete_notebook(client, notebook.id)
        raise
    return notebook


def _topic_prompt(topics: list[Topic], topic_index: int) -> str:
    """Build the prompt that asks for a single topic's details only."""
    return (
        f"This video has a list of topics in order: "
        f"{', '.join(t.name for t in topics)}. "
        f"What did the speaker(s) talk about specifically for the topic "
        f"'{topics[topic_index].name}', and only this topic? "
        f"Start your answer at the point where the previous topic "
        f"'{_topic_boundaries(topics, topic_index)['prev']}' ends and stop before "
        f"the next topic '{_topic_boundaries(topics, topic_index)['next']}' begins, "
        f"so your answer does not overlap with either the previous or the next "
        f"topic. "
        f"Return topic details only, do not include any other text in your response."
    )


async def _get_or_create_topic_list(
    client: NotebookLMClient, ref: VideoRef
) -> tuple[Video, "Notebook | None"]:
    """
    Get the topic list for a video, creating it if it doesn't exist.
    :param client: The NotebookLMClient instance to use for interacting with the notebook service
    :param ref: The video reference (source + id) for which to get or create the topic list
    :return: The video object with its topic list, and the notebook used to generate
             it (or None when the topics were already cached and no notebook was needed)
    """
    video_link = build_video_url(ref)

    logger.info(f"Checking if video {video_link} is already in database...")
    video = await Video.find_one(
        Video.source == ref.source, Video.video_id == ref.video_id
    )
    if video and video.topics:
        video.topics = _validated_topics(video.topics)
        return video, None

    logger.info(
        f"Video {video_link} not found in database, creating notebook and adding source..."
    )

    notebook = await _create_notebook_with_source(client, ref)
    prompt = """
       What are the topics mentioned in the video?
       Return a json containing all topics mentioned in the order they appear in the video using 
       following structure:
       [
           {
                "name": "topic name"
           }
       ]
       
       Return json only, do not include any other text in your response.
       """

    try:
        logger.info(f"Asking notebook {notebook.id} for topics...")
        response = await client.chat.ask(notebook.id, prompt)

        logger.info("Parsing response and saving topics to database...")
        topics_objects = _parse_topics(response.answer)

        if video:
            video.topics = topics_objects
        else:
            video = Video(
                source=ref.source,
                video_id=ref.video_id,
                topics=topics_objects,
            )

        await video.save()
        logger.info(f"Topics saved to database for video {video_link}.")
        return video, notebook
    except Exception:
        await _safe_delete_notebook(client, notebook.id)
        raise


async def get_topic_list(client: NotebookLMClient, ref: VideoRef):
    logger.info(f"Getting topics from {ref}")
    video, notebook = await _get_or_create_topic_list(client, ref)
    if notebook is None:
        return video.topics
    try:
        return video.topics
    finally:
        await _safe_delete_notebook(client, notebook.id)
        logger.info("Deleted temporary notebook used for topic list.")


async def get_topic_details(client: NotebookLMClient, ref: VideoRef, topic_index: int):
    video, notebook = await _get_or_create_topic_list(client, ref)
    topics = _validated_topics(video.topics or [])

    try:
        if topic_index < 0 or topic_index >= len(topics):
            raise IndexError(f"Invalid topic index: {topic_index}")

        if topics[topic_index].detail:
            logger.info("Topic details already exist in database, returning...")
            return {
                "topic_name": topics[topic_index].name,
                "detail": topics[topic_index].detail,
            }

        if notebook is None:
            notebook = await _create_notebook_with_source(client, ref)
        prompt = _topic_prompt(topics, topic_index)
        logger.info("Asking notebook for topic details...")
        response = await client.chat.ask(notebook.id, prompt)

        logger.info("Parsing response and saving details to database...")
        topics[topic_index].detail = response.answer
        await video.save()
        logger.info("Topic detail saved to database.")
        return {
            "topic_name": topics[topic_index].name,
            "detail": topics[topic_index].detail,
        }
    finally:
        if notebook is not None:
            await _safe_delete_notebook(client, notebook.id)
            logger.info("Deleted temporary notebook used for topic details.")


async def get_topic_details_all(client: NotebookLMClient, ref: VideoRef):
    video, notebook = await _get_or_create_topic_list(client, ref)
    topics = _validated_topics(video.topics or [])

    if notebook is None and any(t.detail is None for t in topics):
        notebook = await _create_notebook_with_source(client, ref)

    results = []
    needs_db_save = False
    try:
        for i, topic in enumerate(topics):
            if topic.detail:
                results.append({"topic_name": topic.name, "detail": topic.detail})
            else:
                prompt = _topic_prompt(topics, i)
                logger.info(f"Asking notebook for details on topic: '{topic.name}'...")

                response = await client.chat.ask(notebook.id, prompt)
                topic.detail = response.answer
                needs_db_save = True

                results.append({"topic_name": topic.name, "detail": topic.detail})

        if needs_db_save:
            logger.info(
                f"Saving all newly fetched topic details to database for video {ref} in a "
                f"single batch..."
            )
            await video.save()
            logger.info("Batch save complete.")

        return results
    finally:
        if notebook is not None:
            await _safe_delete_notebook(client, notebook.id)
            logger.info("Deleted temporary notebook used for topic details.")


async def _safe_delete_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    try:
        await client.notebooks.delete(notebook_id)
    except Exception:
        logger.exception(
            "Failed to delete temporary NotebookLM resource %s",
            notebook_id,
            extra={"context": {"notebook_id": notebook_id}},
        )
