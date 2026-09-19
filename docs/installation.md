# Installation and configuration

## 1. Prerequisites

- Python 3.12 or later.
- [uv](https://docs.astral.sh/uv/) to manage dependencies and run the
  application.
- A MongoDB deployment: install it directly, run it with Docker, or use
  MongoDB Atlas.
- `ffmpeg` on `PATH` when summarizing Twitch VODs or X Spaces. `yt-dlp` is
  already a Python dependency of this project.
- A Discord Application with a bot user.
- A Google account that can use NotebookLM.

## 2. Get the source and install dependencies

```bash
git clone https://github.com/homulily85/VSummary.git
cd VSummary
uv sync
```

### MongoDB

Use a MongoDB instance installed directly on the host, a Docker deployment, or
[MongoDB Atlas](https://www.mongodb.com/atlas). Set `MONGODB_URI` to the
connection string for the deployment you choose.

### Install `ffmpeg` for Twitch and X Space

Ubuntu/Debian:

```bash
sudo apt install ffmpeg
```

macOS with Homebrew:

```bash
brew install ffmpeg
```

Twitch VODs and X Spaces are added to NotebookLM as M4A audio files. They must
be public, available to `yt-dlp`, and produce audio within NotebookLM's upload
limit.

## 3. Create and invite the Discord bot

1. Open the Discord Developer Portal and create or select an Application.
2. In **Bot**, create the bot user and reset/copy its token. Keep the token
   secret.
3. In **OAuth2 → URL Generator**, select the `bot` and `applications.commands`
   scopes, then open the generated URL to invite the bot to your server.
4. Grant the bot these permissions in the destination channel:
   - **View Channel**
   - **Send Messages**
   - **Read Message History**
   - **Send Messages in Threads**, when threads are used


## 4. Configure environment variables

Create `.env` from the sample file:

```bash
cp .env.example .env
```

Example local configuration:

```dotenv
DISCORD_TOKEN=replace-with-a-bot-token
MONGODB_URI=mongodb://localhost:27017
MONGODB_DATABASE=VSummary

# Channel for automatic summaries. Leave empty to disable this feature.
AUTO_SUMMARY_CHANNEL_ID=

# Channel for WARNING/ERROR logs. Leave empty for console and file logging only.
DISCORD_LOG_CHANNEL_ID=

# One message per second across the bot.
DISCORD_SEND_INTERVAL_SECONDS=1.0
```

To get a channel ID, enable **Developer Mode** in Discord, right-click the
channel, and choose **Copy Channel ID**. The value must be a positive integer,
without brackets or quotation marks.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | — | Discord bot token. |
| `MONGODB_URI` | Yes | — | MongoDB connection string. |
| `MONGODB_DATABASE` | No | `VSummary` | Database name. |
| `AUTO_SUMMARY_CHANNEL_ID` | No | — | Channel that receives automatic summaries. |
| `DISCORD_LOG_CHANNEL_ID` | No | — | Channel that receives WARNING-and-higher logs. |
| `DISCORD_SEND_INTERVAL_SECONDS` | No | `1.0` | Minimum gap between bot messages; must be greater than 0. |
| `POLL_INTERVAL_MINUTES` | No | `30` | Holodex polling interval for new videos. |
| `SOURCE_RETRY_LIMIT` | No | `5` | Maximum attempts to generate a summary. |
| `DELIVERY_RETRY_LIMIT` | No | `5` | Maximum attempts to deliver an already generated summary to Discord. |
| `HTTP_TIMEOUT_SECONDS` | No | `15.0` | Holodex HTTP timeout. |
| `LOG_FILE_PATH` | No | `logs/vsummary.log` | Daily rotating log-file path. |
| `LOG_LEVEL` | No | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

## 5. Authenticate with NotebookLM

VSummary uses `notebooklm-py`, not a NotebookLM API key. Follow the official
[notebooklm-py installation guide](https://github.com/teng-lin/notebooklm-py/blob/main/docs/installation.md)
to set up its dependencies and authenticated Google session.

Use a separate, throwaway Google account for NotebookLM rather than your
personal or primary account. Treat the account and its session data as dedicated
to this bot, and follow the upstream guide for any future authentication changes.

## 6. Start the bot

```bash
uv run vsummary
```
