import logging

from notebooklm import NotebookLMClient
import json

from vsummary.model.video import Video, Topic


async def _get_or_create_topic_list(client: NotebookLMClient, video_link: str) -> Video:
    logging.info(f"Checking if video is already in database...")
    video = await Video.find_one(Video.link == video_link)
    if video and video.topics:
        return video

    logging.info(f"Video not found in database, creating notebook and adding source...")
    notebook = await client.notebooks.create(video_link)
    await client.sources.add_url(notebook.id, video_link)

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

    logging.info(f"Asking notebook for topics...")
    response = await client.chat.ask(notebook.id, prompt)

    logging.info(f"Parsing response and saving topics to database...")
    topics = json.loads(response.answer)

    topics_objects = []
    for topic in topics:
        topics_objects.append(Topic(name=topic["name"]))

    if video:
        video.notebook_id = notebook.id
        video.topics = topics_objects
    else:
        video = Video(link=video_link, notebook_id=notebook.id, topics=topics_objects)

    await video.save()
    logging.info(f"Topics saved to database.")
    return video


async def get_topic_list(client: NotebookLMClient, video_link: str):
    logging.info(f"Getting topics from {video_link}")
    video = await _get_or_create_topic_list(client, video_link)
    return video.topics


async def get_topic_details(client: NotebookLMClient, video_link: str, topic_index: int):
    video = await _get_or_create_topic_list(client, video_link)
    topics = video.topics

    if topic_index < 0 or topic_index >= len(topics):
        raise IndexError(f"Invalid topic index: {topic_index}")

    if topics[topic_index].detail:
        logging.info(f"Topic details already exist in database, returning...")
        return {"topic_name": topics[topic_index].name, "detail": topics[topic_index].detail}

    notebook = await client.notebooks.get(video.notebook_id)
    prompt = (f"What did the speaker talk about '{topics[topic_index].name}'? Return topic details "
              f"only, do not include any other text in your response.")
    logging.info(f"Asking notebook for topic details...")
    response = await client.chat.ask(notebook.id, prompt)

    logging.info(f"Parsing response and saving details to database...")
    topics[topic_index].detail = response.answer
    await video.save()
    logging.info(f"Topic detail saved to database.")
    return {"topic_name": topics[topic_index].name, "detail": topics[topic_index].detail}


async def get_topic_details_all(client: NotebookLMClient, video_link: str):
    video = await _get_or_create_topic_list(client, video_link)
    topics = video.topics

    results = []
    needs_db_save = False
    notebook = None

    for topic in topics:
        if topic.detail:
            results.append({"topic_name": topic.name, "detail": topic.detail})
        else:
            if notebook is None:
                notebook = await client.notebooks.get(video.notebook_id)

            prompt = (f"What did the speaker talk about '{topic.name}'? Return topic details "
                      f"only, do not include any other text in your response.")
            logging.info(f"Asking notebook for details on topic: '{topic.name}'...")

            response = await client.chat.ask(notebook.id, prompt)
            topic.detail = response.answer
            needs_db_save = True

            results.append({"topic_name": topic.name, "detail": topic.detail})

    if needs_db_save:
        logging.info("Saving all newly fetched topic details to database in a single batch...")
        await video.save()
        logging.info("Batch save complete.")

    return results
