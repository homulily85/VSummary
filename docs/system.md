# System architecture

VSummary is an asynchronous Discord application. It accepts manual video
requests and polls followed YouTube channels for automatic requests. Both
paths persist work in MongoDB, generate NotebookLM content outside the Discord
interaction lifetime, and send the saved output only after generation succeeds.

## Components

```text
Discord command or Holodex poll
             |
             v
   Summarizer / Autosummary cog
             |
             v
 MongoDB durable job collections <----> worker leases
             |                              |
             v                              v
     VideoWorkCoordinator ------------> NotebookLM
             |
             v
 shared Discord rate limiter ---> destination channel
```

`main.py` owns application lifetime: it loads validated settings, configures
logging, opens MongoDB, registers Beanie documents, opens the authenticated
NotebookLM client, and starts `Bot`. `Bot` loads the ping, manual-summary,
slash-command, and auto-summary cogs. It also holds shared services that cogs
use: the NotebookLM client, settings, per-video coordinator, and Discord send
rate limiter.

## Request workflows

### Manual requests

`/topics` and `/detail` normalize the user input into a `VideoRef`. X Space
post URLs are resolved to an archived Space before a job is created. The
command writes a `ManualSummaryJob` and returns immediately so Discord does not
time out while NotebookLM is working.

The manual worker runs every ten seconds. It atomically claims due jobs,
generates topic output or details, persists Discord-sized delivery chunks, and
then sends the chunks to the original request channel. A failed send keeps the
generated chunks and the confirmed chunk index, so the next delivery attempt
resumes instead of generating content again.

### Automatic requests

`/addchannel` stores a Holodex channel in `FollowedChannel`. The auto-summary
poller runs at `POLL_INTERVAL_MINUTES`, obtains streams published after the
channel was added, ignores configured unwanted Holodex topic types, and creates
a `SummaryJob` for each eligible stream. Generation is delayed until at least
two hours after the stream ends to give a transcript time to become available.

The poller claims automatic jobs atomically, renews their lease while working,
generates and persists topics, then delivers the saved chunks to
`AUTO_SUMMARY_CHANNEL_ID`. An expired lease can be reclaimed by another worker
after a restart or interrupted process.

## Persistent data and state

| Collection/model | Purpose | Important invariants |
| --- | --- | --- |
| `Channel` / `FollowedChannel` | Channels followed for automatic summaries. | One record per `channel_id`; `added_at` is UTC. |
| `PendingVideo` / `SummaryJob` | Automatic generation and delivery jobs. | Unique `(channel_id, video_id)`; all scheduling and lease timestamps are UTC. |
| `ManualSummaryJob` | User-requested topic/detail jobs. | Separate source, operation, requester, retry, lease, and delivery checkpoint data. |
| `Video` / `VideoSummary` | Cached NotebookLM topics and details. | Unique `(source, video_id)` cache key. |

Automatic jobs normally move through:

```text
queued -> generating -> ready_to_deliver -> delivering -> completed
                     ^                    |
                     |                    v
                   retry <-------------- failed
```

Manual jobs use the same generation and delivery statuses. Jobs save generated
content before entering delivery. `claimed_by`, `claimed_at`, and
`lease_expires_at` protect automatic jobs from concurrent worker writes; every
write checks that the current worker still owns the lease.

## Concurrency, retries, and rate limits

`VideoWorkCoordinator` is process-local and serializes NotebookLM/cache work
for one normalized `(source, video_id)` pair. It prevents a manual and an
automatic request for the same video from building duplicate temporary
notebooks in one bot process. MongoDB job claims and leases provide durability
across processes and restarts.

Transient source and Discord delivery failures use exponential delays of 1, 2,
4, 8, and so on hours, bounded by `SOURCE_RETRY_LIMIT` and
`DELIVERY_RETRY_LIMIT`. A malformed NotebookLM topic response
(`InvalidSummaryResponse`) gets one immediate retry while the job remains
claimed; only a subsequent failure enters the durable retry queue and consumes
one source retry. Permanent source failures bypass the source retry queue.

Every outbound Discord message passes through the bot's shared rate limiter.
Long output is split into non-empty chunks no longer than Discord's
2,000-character limit, and delivery checkpoints each successful chunk.

## External integrations

- **Discord** supplies slash commands and receives generated summaries.
- **MongoDB/Beanie** stores subscriptions, jobs, leases, delivery checkpoints,
  and cached topic data.
- **Holodex** discovers past YouTube streams for followed channels. Its client
  validates responses and retries transient HTTP and transport errors.
- **NotebookLM** creates temporary notebooks, adds a video URL or audio file,
  produces topic JSON, and generates detailed prose. It authenticates with a
  Google session, not an API key.
- **yt-dlp and ffmpeg** resolve metadata and prepare Twitch VOD or archived X
  Space audio as temporary M4A uploads. Public archived Spaces only are
  supported.

## Observability and operations

Logging is configured before external services are opened. The application
writes UTC console logs and a required daily rotating file log, retaining 14
days. Optional Discord logging queues warning-and-higher events without
blocking the application; it redacts common credential formats and reports
dropped events when its bounded queue recovers. Each Discord log shows an
explicit UTC timestamp and a Discord-rendered local timestamp for the viewer.

Use the command guide for normal bot actions and the installation guide for
settings. Manage persistent schema changes through the deployment process and
preserve a database backup before changing models or indexes.
