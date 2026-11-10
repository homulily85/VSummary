# Installation and configuration

For a container deployment, follow [Run with Docker](#run-with-docker). The
numbered steps below cover a local Python installation.

## Run from source

### 1. Prerequisites

- Python 3.12 or later.
- [uv](https://docs.astral.sh/uv/) to manage dependencies and run the
  application.
- A MongoDB deployment: install it directly, run it with Docker, or use
  MongoDB Atlas.
- `ffmpeg` on `PATH` when summarizing Twitch VODs, X Spaces, other media URLs,
  or using the optional YouTube audio fallback. `yt-dlp` is
  already a Python dependency of this project.
- A Discord Application with a bot user.
- A Google account that can use NotebookLM.

### 2. Get the source and install dependencies

```bash
git clone https://github.com/homulily85/VSummary.git
cd VSummary
uv sync
```

#### MongoDB

Use a MongoDB instance installed directly on the host, a Docker deployment, or
[MongoDB Atlas](https://www.mongodb.com/atlas). Set `MONGODB_URI` to the
connection string for the deployment you choose.

### Install `ffmpeg` for audio uploads

Ubuntu/Debian:

```bash
sudo apt install ffmpeg
```

macOS with Homebrew:

```bash
brew install ffmpeg
```
### 3. Create and invite the Discord bot

1. Open the Discord Developer Portal and create or select an Application.
2. In **Bot**, create the bot user and reset/copy its token. Keep the token
   secret.
3. In **OAuth2 → URL Generator**, select the `bot` and `applications.commands`
   scopes, then open the generated URL to invite the bot to your server.
4. Grant the bot these permissions in the destination channel:
   - **View Channel**
   - **Send Messages**
   - **Send Messages in Threads**, when threads are used

### 4. Configure secrets and operational settings

Create `.env` from the sample file:

```bash
cp .env.example .env
```

```dotenv
DISCORD_TOKEN=replace-with-a-bot-token
MONGODB_URI=replace-with-a-mongodb-uri
HOLODEX_API_KEY=replace-with-a-holodex-api-key
```

Get the required API key from your Holodex account settings. The bot uses it to
look up channels and discover finished streams. See the [Holodex API guide](https://docs.holodex.net/#section/Getting-Started).

```bash
cp config.example.toml config.toml
```

For example:

```toml
[discord]
auto_summary_channel_id = 123456789012345678
send_interval_seconds = 1.0

[logging]
discord_channel_id = 234567890123456789
level = "INFO"
```

Omitted settings use the defaults below.

| TOML option | Default | Purpose |
| --- | --- | --- |
| `discord.auto_summary_channel_id` | Omitted | Destination for automatic summaries. |
| `discord.send_interval_seconds` | `1.0` | Minimum gap between outbound messages. |
| `retries.source_limit` | `5` | Native generation failure threshold. |
| `retries.delivery_limit` | `5` | Discord delivery failure threshold. |
| `retries.initial_delay_seconds` | `3600` | First durable retry delay. |
| `retries.multiplier` | `2` | Multiplier for each subsequent retry delay; at least 1. |
| `retries.invalid_response_retries` | `1` | Immediate malformed-response retries per native generation attempt. |
| `autosummary.transcript_delay_seconds` | `7200` | Delay after stream completion before first generation. |
| `summary.output_language` | `"English"` | Language for generated topic names and details. |
| `youtube.yt_dlp_fallback_enabled` | `false` | One final audio attempt after native retries. Requires `ffmpeg`. |
| `logging.path` | `"logs/vsummary.log"` | Rotating file-log destination. |
| `logging.level` | `"INFO"` | Global minimum severity. |
| `logging.rotation_interval_days` | `1` | File rotation interval in UTC days. |
| `logging.backup_count` | `14` | Retained rotated files; 0 disables deletion of backups. |
| `logging.discord_channel_id` | Omitted | Optional Discord log destination. |
| `logging.discord_queue_size` | `1000` | Maximum queued Discord log events. |
| `logging.discord_min_level` | `"WARNING"` | Discord severity threshold, also subject to the global minimum. |

### 5. Authenticate with NotebookLM

VSummary uses `notebooklm-py`, not a NotebookLM API key. Follow the official
[notebooklm-py installation guide](https://github.com/teng-lin/notebooklm-py/blob/main/docs/installation.md)
to set up its dependencies and authenticated Google session.

Use a separate, throwaway Google account for NotebookLM rather than your
personal or primary account. Treat the account and its session data as dedicated
to this bot, and follow the upstream guide for any future authentication changes.

### 6. Start the bot

```bash
uv run vsummary
```

## Run with Docker

### 1. Set up Google authentication

Use a Google account dedicated to VSummary and follow the
[notebooklm-py master-token guide](https://github.com/teng-lin/notebooklm-py/blob/v0.8.6/docs/installation.md#alternative-master-token-auth-no-cookie-file-to-ship-survives-expiry)
to generate `master_token.json`.

From the deployment directory on the Docker host, run the following as the user
who will run the container. Replace `/path/to/master_token.json` with the path to
the token file on that host.

```bash
install -d -m 700 .notebooklm .notebooklm/profiles .notebooklm/profiles/default logs
install -m 600 /path/to/master_token.json \
  .notebooklm/profiles/default/master_token.json
```

### 2. Pull and start the container

Fill in `DISCORD_TOKEN`, `MONGODB_URI`, and `HOLODEX_API_KEY` in `.env` using the
[example environment file](../.env.example). Use an empty `config.toml` for defaults
or customize the [example configuration](../config.example.toml).

```bash
chmod 600 .env
touch config.toml
VSUMMARY_IMAGE=homulily85/vsummary:1.6.0
docker pull "$VSUMMARY_IMAGE"
docker run -d --name vsummary --init --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  --env-file .env \
  --mount "type=bind,src=$PWD/config.toml,dst=/app/config.toml,readonly" \
  --mount "type=bind,src=$PWD/logs,dst=/app/logs" \
  --mount "type=bind,src=$PWD/.notebooklm,dst=/var/lib/vsummary/notebooklm" \
  "$VSUMMARY_IMAGE"
```