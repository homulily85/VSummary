from notebooklm import NotebookLMClient


async def get_topic_list(client: NotebookLMClient, video_link: str):
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

    response = await client.chat.ask(notebook.id, prompt)
    return response.answer
