# Slash command reference

Commands are synchronized when the bot starts. Type `/` in Discord to use
autocomplete and view Discord's parameter descriptions.

## Manual summaries

| Command | Parameters | Description |
| --- | --- | --- |
| `/topics` | `video` (required) | Queues a topic-list request and posts the plain-text result to the invoking channel. |
| `/detail` | `video` (required), `topic_index` (optional) | Generates detailed content. Omitting `topic_index` generates every topic; providing one generates only that topic. The request is always queued and the result is posted to the invoking channel. |
| `/ping` | None | Confirms that the bot is responding and displays gateway latency. |

### Supported video inputs

- **YouTube:** a valid YouTube URL or video ID, for example `dQw4w9WgXcQ` or
  `https://www.youtube.com/watch?v=dQw4w9WgXcQ`.
- **Twitch:** only a full public VOD URL, for example
  `https://www.twitch.tv/videos/123456789`. Bare VOD IDs, clip URLs, channel
  livestream URLs, and other domains are not accepted.

Examples:

```text
/topics video:https://www.youtube.com/watch?v=dQw4w9WgXcQ
/detail video:dQw4w9WgXcQ topic_index:2
/detail video:https://www.twitch.tv/videos/123456789
```

`topic_index` is one-based. For an invalid index, the bot posts an error after
the worker processes the job. `/topics` does not provide an interactive topic
picker; use `/detail` with `topic_index` to request a specific topic.

### Queue and cache behavior

- `/detail` replies promptly with a queued confirmation. A worker later posts
  the summary as a normal message in the channel, so the interaction does not
  need to remain open.
- `/topics` is queued for both sources. Twitch additionally needs to download
  and convert audio.
- Cache entries are keyed by `(source, video_id)`. Repeated requests can reuse
  existing results instead of creating another NotebookLM notebook.
- Work for the same video is serialized in one bot process to avoid concurrent
  cache writes. Different videos can still be processed independently.

## Automatic-summary channel tracking

| Command | Parameters | Description |
| --- | --- | --- |
| `/addchannel` | `channel_id` (required) | Starts following a Holodex/YouTube channel. |
| `/removechannel` | `channel_id` (required) | Stops following a channel and removes its related pending jobs. |
| `/listchannels` | None | Lists followed channels. |

For these three commands, `channel_id` is a YouTube channel ID (usually starts
with `UC`), not a Discord channel ID or a streamer's display name. For example:

```text
/addchannel channel_id:UCQ0UDLQCjY0rmuxCDE38FGg
/removechannel channel_id:UCQ0UDLQCjY0rmuxCDE38FGg
```

The bot checks for new videos according to `POLL_INTERVAL_MINUTES`. Automatic
summaries are sent to `AUTO_SUMMARY_CHANNEL_ID`, not the channel where
`/addchannel` was invoked. Set this variable to a Discord channel where the bot
has **View Channel** and **Send Messages**.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `The application did not respond` | Confirm the bot is online, its token belongs to the Application that owns the slash command, and the process was restarted after changing the token. |
| `Missing Access` while posting a result | Check the job `channel_id`, **View Channel**/**Send Messages**, and category overrides. |
| A Twitch VOD cannot be processed | Use a full public VOD URL, install `ffmpeg`, confirm the VOD is within the duration limit, and ensure it is reachable from the bot host. |
| NotebookLM is temporarily unavailable | The worker retries up to `SOURCE_RETRY_LIMIT`; inspect the log file or `DISCORD_LOG_CHANNEL_ID` for details. |
