# Slash command reference

Commands are synchronized when the bot starts. Type `/` in Discord to use
autocomplete and view Discord's parameter descriptions.

## Manual summaries

| Command | Parameters | Description |
| --- | --- | --- |
| `/topics` | `video` (required) | Queues a topic-list request and posts the plain-text result to the invoking channel. |
| `/detail` | `video` (required), `topic_index` (optional) | Generates detailed content. Omitting `topic_index` generates every topic; providing one generates only that topic. The request is always queued and the result is posted to the invoking channel. |
| `/queue` | None | Lists active auto-summary and manual-summary jobs in processing order. |
| `/ping` | None | Confirms that the bot is responding and displays gateway latency. |

### Supported video inputs

- **YouTube:** a valid YouTube URL or video ID, for example `dQw4w9WgXcQ` or
  `https://www.youtube.com/watch?v=dQw4w9WgXcQ`.
- **Twitch:** a full public VOD URL, for example
  `https://www.twitch.tv/videos/123456789`. Bare VOD IDs are not accepted.
- **X (Twitter) Space:** a public archived Space URL such as
  `https://x.com/i/spaces/1OwxWwQOPlNxQ`, or an X post URL whose media resolves
  to a Space. `twitter.com`/`x.com` plus `www`, `m`, and `mobile` variants and
  query parameters are accepted. Upcoming, live, replay-disabled, and
  not-yet-archived Spaces are rejected before a job is queued.
- **Other media:** a public HTTP(S) URL for one finished video or audio item
  accessible to `yt-dlp`, including Twitch clips and public video posts.
  Playlist, live, upcoming, private, and unsupported URLs are rejected.
  Support depends on the site and may change when the site changes.

Examples:

```text
/topics video:https://www.youtube.com/watch?v=dQw4w9WgXcQ
/detail video:dQw4w9WgXcQ topic_index:2
/detail video:https://www.twitch.tv/videos/123456789
/topics video:https://x.com/i/spaces/1OwxWwQOPlNxQ
/topics video:https://vimeo.com/123456789
```

`topic_index` is one-based. For an invalid index, the bot posts an error after
the worker processes the job. `/topics` does not provide an interactive topic
picker; use `/detail` with `topic_index` to request a specific topic.

## Automatic-summary channel tracking

| Command | Parameters | Description |
| --- | --- | --- |
| `/addchannel` | `channel_id` (required) | Starts following a Holodex/YouTube channel. |
| `/removechannel` | `channel_id` (required) | Stops following a channel and removes its related pending jobs. |
| `/listchannels` | None | Lists followed channels. |
| `/addignoredtopic` | `topic_id` (required) | Prevents future videos with this Holodex topic from creating auto-summary jobs. |
| `/removeignoredtopic` | `topic_id` (required) | Allows future videos with this Holodex topic to create auto-summary jobs. |
| `/listignoredtopics` | None | Lists Holodex topics currently ignored by auto-summary. |

For `/addchannel`, `/removechannel`, and `/listchannels`, `channel_id` is a
YouTube channel ID (usually starts with `UC`), not a Discord channel ID or a
streamer's display name. For example:

```text
/addchannel channel_id:UCQ0UDLQCjY0rmuxCDE38FGg
/removechannel channel_id:UCQ0UDLQCjY0rmuxCDE38FGg
```

The bot checks for new videos according to `POLL_INTERVAL_MINUTES`. Automatic
summaries are sent to `AUTO_SUMMARY_CHANNEL_ID`, not the channel where
`/addchannel` was invoked. Set this variable to a Discord channel where the bot
has **View Channel** and **Send Messages**.

Topic-policy commands apply globally to this bot instance. Configure which
users, roles, and channels may use them in Discord's **Server Settings →
Integrations**. Their `topic_id` value is the Holodex topic classification, not
a free-form video topic. Topic IDs are matched case-insensitively. The initial
ignored topics are `Original_Song`, `membersonly`, `shorts`, and `Music_Cover`;
server administrators can remove any of them.

Policies are evaluated against a video's publication time, not the time the
bot polls it. Adding a topic ignores only videos published after the command;
removing it allows only videos published after that command. Before generating
an automatic summary, the bot checks the video's Holodex topic again. A queued
job is cancelled without posting if Holodex has since classified the video
under a topic ignored at its publication time. If Holodex cannot provide a
topic, the bot retries the lookup once and then continues with auto-summary.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `The application did not respond` | Confirm the bot is online, its token belongs to the Application that owns the slash command, and the process was restarted after changing the token. |
| `Missing Access` while posting a result | Check the job `channel_id`, **View Channel**/**Send Messages**, and category overrides. |
| A Twitch VOD cannot be processed | Use a full public VOD URL, install `ffmpeg`, and ensure its converted audio fits NotebookLM's upload limit. |
| An X Space cannot be processed | Use a public archived Space (or a post that resolves to one), install `ffmpeg`, and ensure its converted audio fits NotebookLM's upload limit. |
| Another media URL cannot be processed | Use a public URL for one completed recording supported by `yt-dlp`, install `ffmpeg`, and ensure its audio fits NotebookLM's upload limit. |
| NotebookLM is temporarily unavailable | The worker retries up to `SOURCE_RETRY_LIMIT`; inspect the log file or `DISCORD_LOG_CHANNEL_ID` for details. |
