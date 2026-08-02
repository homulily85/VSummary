from rich import json
import logging

from notebooklm import NotebookLMClient
from src.model.video import Video, Topic


async def get_topic_list(client: NotebookLMClient, video_link: str):
    logging.info(f"Getting topics from {video_link}")
    logging.info(f"Checking if video is already in database...")
    video = await Video.find_one(Video.link == video_link)
    if video and video.topics:
        return video.topics

    logging.info(f"Video not found in database, creating notebook and adding source...")
    notebook = await client.notebooks.create(video_link)
    await client.sources.add_url(notebook.id, video_link)

    prompt = """
       What are the topics mentioned in the video?
       Return a json containing all topics mentioned in the video using following structure:
       [
           {
                "index": "index of topic (0-based) in the order they appear in the video",
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
        topics_objects.append(Topic(index=topic["index"], name=topic["name"]))

    video = Video(link=video_link, notebook_id=notebook.id, topics=topics_objects)
    await video.save()

    logging.info(f"Topics saved to database.")
    return video.topics
