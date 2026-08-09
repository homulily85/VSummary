import json
import logging
from typing import TYPE_CHECKING

from notebooklm import NotebookLMClient, SourceAddError

from vsummary.model.video import Topic, Video
from vsummary.util.video import VideoRef, build_video_url

if TYPE_CHECKING:
    from notebooklm.types import Notebook

logger = logging.getLogger(__name__)


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
    except SourceAddError:
        await client.notebooks.delete(notebook.id)
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
    video = await Video.find_one(Video.source == ref.source, Video.video_id == ref.id)
    if video and video.topics:
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

    logger.info(f"Asking notebook {notebook.id} for topics...")
    response = await client.chat.ask(notebook.id, prompt)

    logger.info("Parsing response and saving topics to database...")
    topics = json.loads(response.answer)

    topics_objects = []
    for topic in topics:
        topics_objects.append(Topic(name=topic["name"]))

    if video:
        video.topics = topics_objects
    else:
        video = Video(
            source=ref.source,
            video_id=ref.id,
            topics=topics_objects,
        )

    await video.save()
    logger.info(f"Topics saved to database for video {video_link}.")
    return video, notebook


async def get_topic_list(client: NotebookLMClient, ref: VideoRef):
    logger.info(f"Getting topics from {ref}")
    video, notebook = await _get_or_create_topic_list(client, ref)
    if notebook is None:
        return video.topics
    try:
        return video.topics
    finally:
        await client.notebooks.delete(notebook.id)
        logger.info("Deleted temporary notebook used for topic list.")


async def get_topic_details(client: NotebookLMClient, ref: VideoRef, topic_index: int):
    video, notebook = await _get_or_create_topic_list(client, ref)
    topics = video.topics

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
            await client.notebooks.delete(notebook.id)
            logger.info("Deleted temporary notebook used for topic details.")


async def get_topic_details_all(client: NotebookLMClient, ref: VideoRef):
    video, notebook = await _get_or_create_topic_list(client, ref)
    topics = video.topics

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
            await client.notebooks.delete(notebook.id)
            logger.info("Deleted temporary notebook used for topic details.")
