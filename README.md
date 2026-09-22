# VSummary

VSummary is a Discord bot for Vtuber fans who want to catch up on long streams,
zatsudan, game sessions, announcements, and other video content without losing
the important context. It uses NotebookLM to turn YouTube videos and Twitch VODs
into topic lists and detailed, readable summaries right in Discord.

Follow your favourite Vtuber channels for automatic recaps, or request a summary
for a video shared in your server.

## Features

- **Catch up on streams faster** — get a topic list or detailed explanation for
  a YouTube video or public Twitch VOD.
- **Ask for exactly what you need** — use `/detail` for one topic or for the
  complete video summary.
- **Follow favourite Vtubers** — track Holodex/YouTube channels and post new
  video summaries automatically to a Discord channel.


## Supported sources

| Source | Accepted input | Notes |
| --- | --- | --- |
| YouTube | A video URL or video ID | Supports `/topics` and `/detail`. |
| Twitch | A full public VOD URL | Requires `ffmpeg`; clips and bare VOD IDs are not supported. |
| X (Twitter) Space | A public archived Space URL or post URL containing a Space | Supports `/topics` and `/detail`; requires `ffmpeg`. Upcoming, live, and unarchived Spaces are rejected. |

## How it works

1. Use `/topics` to receive a topic list for a video.
2. Use `/detail` with an optional topic index to request a focused explanation
   or a full summary.
3. The bot posts the completed result back into the channel where the request
   was made.

For automatic recaps, add a Vtuber's YouTube channel with `/addchannel` and set
the destination channel with `AUTO_SUMMARY_CHANNEL_ID`.

## Documentation

- [Installation, configuration, and deployment](docs/installation.md)
- [Slash command reference](docs/commands.md)
- [System architecture and operations](docs/system.md)
