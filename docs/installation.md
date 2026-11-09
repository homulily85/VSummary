# Installation and configuration

For a container deployment, follow [Run with Docker](#run-with-docker). The
numbered steps below cover a local Python installation.

## 1. Prerequisites

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

### Install `ffmpeg` for audio uploads

Ubuntu/Debian:

```bash
sudo apt install ffmpeg
```

macOS with Homebrew:

```bash
brew install ffmpeg
```

Twitch VODs, X Spaces, and other public media URLs are added to NotebookLM as
M4A audio files. They must be available to `yt-dlp` and produce audio within
NotebookLM's upload limit. Each request must refer to one completed recording.
For a bot exposed to untrusted users, block outbound connections to private
networks at the host or container network boundary; media sites can redirect
requests to other hosts.

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


## 4. Configure secrets and operational settings

Create `.env` from the sample file:

```bash
cp .env.example .env
```

Only application secrets belong in `.env` (or the process environment, which
takes precedence):

```dotenv
DISCORD_TOKEN=replace-with-a-bot-token
MONGODB_URI=mongodb://localhost:27017
```

Operational options are read only from `config.toml` in the working directory.
The file is optional and ignored by Git. To customize the defaults:

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

Omitted settings use the defaults below. Omit either channel ID to disable that
destination; TOML has no `null` value. Relative paths remain relative to the
working directory. Restart the bot after editing configuration. Unknown keys,
invalid types, and malformed TOML stop startup before external services open.
Programmatic callers may pass `config_path` to `load_settings`; an explicitly
selected missing file is an error, while an absent default file uses defaults.

To get a channel ID, enable **Developer Mode** in Discord, right-click the
channel, and choose **Copy Channel ID**. The value must be a positive integer,
without brackets or quotation marks.

| TOML option | Default | Purpose |
| --- | --- | --- |
| `discord.auto_summary_channel_id` | Omitted | Destination for automatic summaries. |
| `discord.send_interval_seconds` | `1.0` | Minimum gap between outbound messages. |
| `workers.poll_interval_minutes` | `30` | Automatic-summary polling interval. |
| `workers.manual_poll_interval_seconds` | `10` | Manual worker interval. |
| `workers.lease_seconds` | `900` | Shared job lease duration; renewed every third of the duration. |
| `retries.source_limit` | `5` | Native generation failure threshold. |
| `retries.delivery_limit` | `5` | Discord delivery failure threshold. |
| `retries.initial_delay_seconds` | `3600` | First durable retry delay. |
| `retries.multiplier` | `2` | Multiplier for each subsequent retry delay; at least 1. |
| `retries.invalid_response_retries` | `1` | Immediate malformed-response retries per native generation attempt. |
| `autosummary.transcript_delay_seconds` | `7200` | Delay after stream completion before first generation. |
| `summary.output_language` | `"English"` | Language for generated topic names and details. |
| `youtube.yt_dlp_fallback_enabled` | `false` | One final audio attempt after native retries. Requires `ffmpeg`. |
| `media.max_audio_mib` | `190` | Maximum prepared audio size in MiB. |
| `media.bitrate_kbps` | `64` | Converted M4A bitrate. |
| `media.download_format` | `"bestaudio/best"` | yt-dlp source format selector. |
| `media.upload_timeout_seconds` | `600` | Wait for uploaded audio processing. |
| `logging.path` | `"logs/vsummary.log"` | Rotating file-log destination. |
| `logging.level` | `"INFO"` | Global minimum severity. |
| `logging.rotation_interval_days` | `1` | File rotation interval in UTC days. |
| `logging.backup_count` | `14` | Retained rotated files; 0 disables deletion of backups. |
| `logging.discord_channel_id` | Omitted | Optional Discord log destination. |
| `logging.discord_queue_size` | `1000` | Maximum queued Discord log events. |
| `logging.discord_min_level` | `"WARNING"` | Discord severity threshold, also subject to the global minimum. |

Numbers must be finite and positive, except retry counts, transcript delay, and
backup count may be zero. A source/delivery threshold of zero schedules no
durable retries after the initial failure. Logging levels accept `DEBUG`,
`INFO`, `WARNING`, `ERROR`, or `CRITICAL`, ignoring case. Booleans use TOML
`true`/`false`, without quotes. Shared media settings apply to Twitch, archived
X Spaces, generic media, and the YouTube audio fallback.

### Summary output language and cache upgrade

To generate topics and details in another language, add this to `config.toml`
and restart the bot:

```toml
[summary]
output_language = "Vietnamese"
```

The default is `"English"`. Use a nonblank language name, such as `"Japanese"`
or `"Traditional Chinese"`. Surrounding whitespace is removed. Language names
are user-supplied labels rather than a fixed registry; aliases such as `"en"`
and `"English"` have separate caches. Language selection is available only in
TOML and controls generated content; Discord commands, status text, and logs
keep their existing language.

Each video has a separate topic/detail cache per language. Cache keys ignore
case, so `"Japanese"` and `"JAPANESE"` reuse the same output. Switching back to
a previous language reuses its completed topics and details. Pending generation
uses the current setting after a restart, including generation retries and the
YouTube audio fallback. Already saved job output is delivered unchanged, even
if the setting changes before delivery succeeds. Completed and failed jobs are
not requeued by a language change.

Stop older bot processes before upgrading to language-specific caching. Startup
creates the unique `(source, video_id, output_language)` cache index before
removing the obsolete unique `(source, video_id)` index, preserving other
indexes. This transition can run repeatedly and must succeed before workers
start. Existing cache records without a language tag are preserved but not
reused, including for English; the first request creates a new language-tagged
cache without guessing the language of older content.

### Migrating an existing `.env`

Non-secret environment options are no longer read. Move their values using
this mapping, then remove the old entries from `.env` and any service environment:

| Old environment variable | TOML option |
| --- | --- |
| `AUTO_SUMMARY_CHANNEL_ID` | `discord.auto_summary_channel_id` |
| `DISCORD_SEND_INTERVAL_SECONDS` | `discord.send_interval_seconds` |
| `POLL_INTERVAL_MINUTES` | `workers.poll_interval_minutes` |
| `SOURCE_RETRY_LIMIT` | `retries.source_limit` |
| `DELIVERY_RETRY_LIMIT` | `retries.delivery_limit` |
| `YOUTUBE_YT_DLP_FALLBACK_ENABLED` | `youtube.yt_dlp_fallback_enabled` |
| `LOG_FILE_PATH` | `logging.path` |
| `LOG_LEVEL` | `logging.level` |
| `DISCORD_LOG_CHANNEL_ID` | `logging.discord_channel_id` |

Convert quoted environment numbers to TOML numbers and legacy boolean strings
to TOML booleans; omit empty channel IDs. `MONGODB_DATABASE` and
`HTTP_TIMEOUT_SECONDS` are unsupported: the database is fixed to `VSummary`,
and Holodex retains its existing endpoint, headers, 15-second timeout, retries,
pagination, and topic-refresh policy. `APP_ID`, `PUBLIC_KEY`, and
`TWITCH_MAX_DURATION_SECONDS` are unused legacy entries and do not need TOML options.

### Optional YouTube audio fallback

To try downloaded audio when NotebookLM cannot generate a YouTube summary, add
this to `config.toml` and restart the bot:

```toml
[youtube]
yt_dlp_fallback_enabled = true
```

The option is disabled by default. Use a native TOML boolean.

Manual `/topics` and `/detail` requests and automatic summaries first use the
normal YouTube path, including `retries.source_limit` and its backoff. If a
retryable NotebookLM generation failure exhausts that budget, the job queues
one final attempt for the next worker pass. That attempt uses the existing
`yt-dlp` downloader and `ffmpeg` to prepare M4A audio, uploads it to NotebookLM,
and generates the requested output. The recording must be accessible to
`yt-dlp`, completed, and within the existing audio upload size limit.

The audio attempt receives no immediate malformed-response retry or additional
generation retries. Successful output is saved before Discord delivery, so
delivery failures can still retry without downloading or generating again.
Existing cached topics, completed details, and the YouTube cache key are retained.

Permanent errors, invalid topic indices, ignored-video cancellations, Holodex
errors, and Discord delivery errors do not trigger this fallback. Enabling it
affects active and new jobs; already failed jobs are not requeued. The fallback
phase survives restarts. Disabling the option before a queued fallback runs
finishes that exhausted job without downloading audio.

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

## Run with Docker

Use Docker Engine or Docker Desktop with Linux containers on AMD64. Pull the
published `homulily85/vsummary` image from Docker Hub. It includes the Python
runtime and audio tools. Have a reachable MongoDB deployment and
[create and invite your Discord bot](#3-create-and-invite-the-discord-bot)
before starting the container. For an existing database from before 1.6,
complete the [offline migration](migration-1.6.md) first.

### Set up Google authentication

On a workstation with a browser, install the same `notebooklm-py` version as
the bot. Use a Google account dedicated to VSummary.

```bash
uv tool install 'notebooklm-py[browser,headless]==0.8.6'
notebooklm --profile default login --master-token --account YOUR_GOOGLE_EMAIL
```

Sign in through the browser window. The CLI saves `master_token.json` under
`~/.notebooklm/profiles/default/`, or under the corresponding path in
`NOTEBOOKLM_HOME` if set. Run this command with `NOTEBOOKLM_AUTH_JSON` unset.
See the [upstream login guide](https://github.com/teng-lin/notebooklm-py/blob/v0.8.6/docs/installation.md#alternative-master-token-auth-no-cookie-file-to-ship-survives-expiry)
for browser setup and troubleshooting.

On the Docker host, choose a deployment directory and run the remaining
commands there. Copy the master token securely from the workstation. The
`install` command below uses the default source path when the workstation is
also the Docker host.

```bash
(umask 077; mkdir -p .notebooklm/profiles/default logs)
chmod 700 .notebooklm .notebooklm/profiles .notebooklm/profiles/default
install -m 600 ~/.notebooklm/profiles/default/master_token.json \
  .notebooklm/profiles/default/master_token.json
```

The container creates `storage_state.json` alongside the master token on first
start. NotebookLM uses that file for the active Google cookies and updates it
as the session refreshes. Both files grant access to the Google account. Keep
them private, keep their mode at `0600`, and give each running bot its own
profile directory. If a master token leaks, revoke its corresponding session
in your Google Account security settings. See the
[upstream cookie lifecycle guide](https://github.com/teng-lin/notebooklm-py/blob/v0.8.6/docs/auth-cookie-lifecycle.md).

### Pull and start the container

Create a `.env` file with these two lines and replace the placeholders. Set
the file's permissions to `0600`. Keep Google credentials in the profile
directory.

```dotenv
DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
MONGODB_URI=YOUR_MONGODB_CONNECTION_STRING
```

Create an empty `config.toml` with `touch config.toml` to use defaults, or
copy the [example configuration](../config.example.toml) and edit it. Keep
the configured file log under `/app/logs` for persistence.

`MONGODB_URI` must use an address reachable from the container. If MongoDB is
on the Docker host, use `host.docker.internal` in the URI. With Docker Engine
on Linux, add `--add-host=host.docker.internal:host-gateway` to `docker run` and
ensure MongoDB listens on an address reachable from the Docker bridge.

On Linux or WSL, run from the deployment directory as a regular user who owns the
mounted files and directories:

```bash
export VSUMMARY_IMAGE=homulily85/vsummary:1.6.0
docker pull "$VSUMMARY_IMAGE"
docker run -d --name vsummary --init --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  --env-file .env \
  --mount "type=bind,src=$PWD/config.toml,dst=/app/config.toml,readonly" \
  --mount "type=bind,src=$PWD/logs,dst=/app/logs" \
  --mount "type=bind,src=$PWD/.notebooklm,dst=/var/lib/vsummary/notebooklm" \
  "$VSUMMARY_IMAGE"
```

### Check and maintain the container

```bash
docker logs -f vsummary
docker exec vsummary notebooklm auth check --test --passive
```

Use `/ping` in Discord to check that the bot is online. Use
`docker stop vsummary` and `docker start vsummary` to stop and resume with the
existing settings. After changing `config.toml` in place, restart the bot. To
upgrade, set `VSUMMARY_IMAGE` to the new versioned image and pull it. Stop and
remove the old container with `docker stop vsummary` and `docker rm vsummary`,
then repeat `docker run` with the same mounts. Use the same recreation steps
after changing `.env`. The host retains the profile and logs.

If Google revokes the master token, stop the bot and repeat the workstation
login. Replace the mounted `master_token.json`, remove the old
`storage_state.json` for that profile, and start the container so it can mint a
new session.

### Build locally and publish releases

For development, build from a source checkout with
`docker build -t vsummary:local .`. Use `vsummary:local` as `VSUMMARY_IMAGE`
and go straight to the `docker run` step.

Before the first publish, create the public Docker Hub repository
`homulily85/vsummary`. In the GitHub repository's Actions settings, set the
variable `DOCKERHUB_USERNAME` to `homulily85` and add `DOCKERHUB_TOKEN` as a
secret with Docker Hub Read & Write access.

Publish a stable GitHub Release from `main` with a tag such as `v1.6.0` that
matches the project version. The Docker workflow tests the image, pushes its
version tag, then updates `latest`. It verifies that both tags resolve to the
same digest and that the versioned image can be pulled anonymously.
