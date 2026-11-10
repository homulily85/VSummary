# Slash command reference

## Manual summaries

| Command | Parameters | Description |
| --- | --- | --- |
| `/topics` | `video_url_or_id` (required) | Queues a topic-list request and posts numbered pages to the invoking channel. |
| `/detail` | `video_url_or_id` (required), `topic_index` (optional) | Generates detailed content. Omitting `topic_index` generates every topic; providing one generates only that topic. |
| `/deletecache` | `video_url_or_id` (required) | Deletes one video's cached topics and details across all languages.|
| `/queue` | None | Lists active auto-summary and manual-summary jobs.|
| `/ping` | None | Confirms that the bot is responding and displays gateway latency. |

### Supported video inputs

- **YouTube:** a valid YouTube URL or video ID, for example `dQw4w9WgXcQ` or
  `https://www.youtube.com/watch?v=dQw4w9WgXcQ`.
- **Twitch:** a full public VOD URL, for example
  `https://www.twitch.tv/videos/123456789`. Bare VOD IDs are not accepted.
- **X (Twitter) Space:** a public archived Space URL such as
  `https://x.com/i/spaces/1OwxWwQOPlNxQ`, or an X post URL whose media resolves
  to a Space.
- **Other media:** a public HTTP(S) URL for one finished video or audio item
  accessible to `yt-dlp`, including Twitch clips and public video posts.

## Automatic-summary channel tracking

| Command | Parameters | Description |
| --- | --- | --- |
| `/addchannel` | `channel_id` (required) | Starts following a Holodex/YouTube channel. |
| `/removechannel` | `channel_id` (required) | Stops following a channel and removes its related pending jobs. |
| `/listchannels` | None | Lists followed channels. |
| `/addignoredtopic` | `topic_id` (required) | Prevents future videos with this Holodex topic from creating auto-summary jobs. |
| `/removeignoredtopic` | `topic_id` (required) | Allows future videos with this Holodex topic to create auto-summary jobs. |
| `/listignoredtopics` | None | Lists Holodex topics currently ignored by auto-summary. |
