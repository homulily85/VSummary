# AGENT.md

Guidance for AI coding agents (and humans) working on **vsummary** — a Discord
bot that summarizes videos using NotebookLM.

## Project overview

- **Name:** `vsummary`
- **Purpose:** Discord bot that takes video links/content, feeds them into
  NotebookLM, and posts back a summary (and possibly other NotebookLM
  artifacts — podcasts, reports, quizzes, etc.) in Discord.
- **Language / runtime:** Python >= 3.12
- **Package manager / build backend:** `uv` (see `uv.lock`) with
  `setuptools` as the PEP 517 build backend.
- **Entry point:** console script `vsummary` → `vsummary.main:main`
  (see `[project.scripts]` in `pyproject.toml`).

## Testing policy (mandatory — read before writing any code)

This project requires a **test-first** workflow. Follow this loop for
every change, no exceptions:

1. **Write the test(s) first.** Before implementing or modifying any
   feature, add or update the closest relevant file under `tests/`
   (`test_settings.py`, `test_migrations.py`, `test_models.py`,
   `test_refactored_models.py`, `test_holodex.py`, `test_video.py`,
   `test_youtube.py`, `test_formatting.py`, or `test_workflow_state.py`).
   Use `test_integration.py` only when coverage genuinely spans multiple
   boundaries. The test(s) should express the desired behavior and are
   expected to **fail** at this point since the implementation doesn't
   exist/isn't updated yet.
2. **Confirm the new test fails** for the expected reason:
   `uv run pytest tests/test_<module>.py -v`. If it passes immediately,
   the test isn't actually exercising the new behavior — fix the test.
3. **Implement (or modify) the feature** in `src/vsummary/...` to make the
   test pass. Keep the implementation focused on satisfying the test(s)
   plus the actual requirements — don't gold-plate.
4. **Run the full suite, not just the new test:**
   ```bash
   uv run pytest
   ```
   Every test must pass — both the new/updated tests and every
   pre-existing test in `tests/`. A change is not complete if it breaks
   an unrelated test; either the change or the affected test needs to be
   fixed (never silently delete/skip a test to make the suite green
   without understanding why it broke).
5. **Lint before wrapping up:** `uv run ruff check --fix .` and
   `uv run ruff format .`.

Rules of thumb:
- **New feature or new module** (e.g. a new cog, a new `util/` helper) →
  create a corresponding `tests/test_<module>.py` first, with tests for
  the intended public behavior (happy path + at least one edge case /
  error case), before writing the implementation.
- **Modifying existing code** (`bot.py`, an existing cog, `util/summarizer.py`,
  `util/youtube.py`, `model/video.py`, etc.) → first update or extend the
  closest focused test file (`test_workflow_state.py`, `test_holodex.py`,
  `test_video.py`, `test_youtube.py`, or another file listed in the layout)
  to cover the new/changed behavior, confirm it fails against the old
  implementation, then change the implementation.
- **Bug fixes** → write a regression test that reproduces the bug first
  (it should fail), then fix the code so it passes.
- Discord- and NotebookLM-facing code should be tested with mocks/fakes
  for `discord.py` objects and the `notebooklm` client (do not hit real
  Discord or NotebookLM/Google services in tests) — check `tests/`
  for the existing mocking patterns and reuse them rather than
  inventing a new style.
- Never mark a task/feature as done, and never report success to the
  user, unless `uv run pytest` has been run and every test passes and
  `ruff check`/`ruff format` are clean.

## Tech stack

| Concern              | Library         |
|-----------------------|-----------------|
| Discord bot framework | `discord.py`    |
| NotebookLM automation | `notebooklm-py` |
| Persistence / ODM     | `beanie` (MongoDB, async, built on Motor + Pydantic) |
| Config / secrets      | `python-dotenv` |
| Linting               | `ruff`          |
| Git hooks             | `pre-commit`    |

## Project layout

```
src/vsummary/
├── main.py              # process entry point; loads settings, runs migrations, owns resources
├── settings.py          # validated runtime configuration and .env loading
├── migrations.py        # versioned, idempotent MongoDB data migrations
├── bot.py               # Bot subclass, dependency injection, cog loading, shutdown
├── cogs/                # Discord adapters and slash-command handlers
│   ├── misc/
│   │   └── ping.py      # simple health-check / latency command
│   ├── summarizer/
│   │   └── summarizer.py# /topics and /detail commands plus interactive topic UI
│   └── autosummary/
│       └── autosummary.py # channel commands and durable Holodex polling workflow
├── model/
│   ├── video.py          # Topic and VideoSummary documents; legacy Video compatibility model
│   └── channel.py        # FollowedChannel, SummaryJob, and job-state models
└── util/
    ├── discord.py         # Discord message chunking within the 2,000-character limit
    ├── holodex.py         # typed Holodex client/gateway, retries, and response parsing
    ├── summarizer.py      # NotebookLM summary service and topic-detail workflow
    ├── video.py           # source-agnostic VideoRef parsing and URL construction
    └── youtube.py         # YouTube URL/ID normalization and validation

tests/
├── test_formatting.py       # Discord message chunking
├── test_holodex.py          # Holodex client contracts and retry behavior
├── test_integration.py      # mixed Discord, NotebookLM, and autosummary coverage
├── test_migrations.py       # database migration behavior
├── test_models.py           # legacy model compatibility behavior
├── test_refactored_models.py# current model names, states, and validation
├── test_settings.py         # settings parsing and validation
├── test_video.py            # source-agnostic VideoRef behavior
├── test_workflow_state.py   # autosummary generation/delivery state transitions
└── test_youtube.py          # YouTube parsing and validation
```

Notes for agents:
- There are **two files named `summarizer.py`** with different roles:
  `cogs/summarizer/summarizer.py` (Discord command/cog layer — parses user
  input, replies in Discord) and `util/summarizer.py` (NotebookLM summary
  service and topic workflow — has no Discord dependency). Keep this
  separation: cogs should stay thin and delegate to `util/`.
- `__pycache__` directories and `*.egg-info` are build artifacts — never
  edit or commit meaningful changes there; they're regenerated automatically.
- New Discord features should be added as new cogs under `src/vsummary/cogs/`
  and registered/loaded in `bot.py`.
- Data models backed by MongoDB go in `src/vsummary/model/` as Beanie
  `Document` subclasses. `main.py` registers `FollowedChannel`, `SummaryJob`,
  and `VideoSummary` with `beanie.init_beanie(...)` during startup.
- `FollowedChannel` and `SummaryJob` preserve the existing `Channel` and
  `PendingVideo` collection names. `Channel`, `PendingVideo`, and `Video` are
  compatibility models/aliases; prefer the current names in new code.
- `VideoSummary` has a unique `(source, video_id)` index. `SummaryJob` has a
  unique `(channel_id, video_id)` index plus status and `next_attempt_at`
  indexes. Do not bypass these constraints with check-then-insert logic.
- `SummaryJob` uses `JobStatus` values `queued`, `generating`,
  `ready_to_deliver`, `delivering`, `completed`, and `failed`. Generation
  output is persisted before Discord delivery so delivery retries do not
  regenerate NotebookLM output.
- `util/youtube.py` is YouTube-only. Use `util/video.py` for the
  source-agnostic `VideoRef` boundary and `util/summarizer.py` for NotebookLM
  work. Keep both free of Discord dependencies so they remain unit-testable.
- `tests/` reflects the current focused coverage plus the existing mixed
  `test_integration.py`; add focused tests beside the closest existing test
  file rather than assuming the old `test_cogs.py`/`test_summarizer.py`
  layout.

### Migrations

Migrations currently run automatically during `vsummary.main.async_main`,
after connecting to MongoDB and before Beanie initialization. There is no
separate migration executable. To run them independently, import
`migrate_database` from `vsummary.migrations` and call it with an async
PyMongo database handle.

The migration path is additive and idempotent. It preserves the existing
`Channel`, `PendingVideo`, and `Video` collections, maps legacy `done` jobs to
`completed`, fills new job fields, renames legacy `Video.id` to `video_id`,
and records the applied schema version in `vsummary_migrations`. It does not
drop collections or deduplicate records automatically. Back up production
data and inspect duplicates before starting a deployment that creates the new
unique indexes.

## Setup & common commands

This project uses `uv`. Prefer `uv` commands over raw `pip`/`python` so the
lockfile (`uv.lock`) stays authoritative.

```bash
# Install dependencies (incl. dev group) into a local .venv
uv sync

# Run the bot
uv run vsummary
# or
uv run python -m vsummary.main

# Add a runtime dependency
uv add <package>

# Add a dev-only dependency
uv add --dev <package>

# Lint (and format check) with ruff
uv run ruff check .
uv run ruff format --check .

# Auto-fix lint issues / format
uv run ruff check --fix .
uv run ruff format .

# Install git hooks (pre-commit)
uv run pre-commit install

# Run pre-commit on all files
uv run pre-commit run --all-files

# Run the full test suite
uv run pytest

# Run a single test file
uv run pytest tests/test_workflow_state.py

# Run a single test by name, with verbose output
uv run pytest tests/test_youtube.py::test_extract_video_id -v

# Run tests with coverage (if pytest-cov is installed)
uv run pytest --cov=vsummary --cov-report=term-missing
```

Tests live in `tests/` and use `pytest` (see `pyproject.toml` for the
`pytest`/`pytest-asyncio`/etc. dev dependencies actually pinned — check
before assuming plugin availability, e.g. whether async tests use
`pytest-asyncio` markers or `anyio`). **Always run tests with `uv run
pytest`, not a bare `pytest` or `python -m pytest`**, so the project's
locked virtual environment is used.

## Environment / configuration

- Config/secrets are loaded once at startup via `python-dotenv`; a local
  untracked `.env` file is expected. `settings.py` validates configuration
  before the bot starts, so do not read environment variables directly from
  cogs or utility modules.
- Required variables are `DISCORD_TOKEN` and `MONGODB_URI`.
- Optional variables are `MONGODB_DATABASE` (default `VSummary`),
  `AUTO_SUMMARY_CHANNEL_ID`, `POLL_INTERVAL_MINUTES` (default `30`),
  `SOURCE_RETRY_LIMIT` (default `5`), `DELIVERY_RETRY_LIMIT` (default `5`),
  and `HTTP_TIMEOUT_SECONDS` (default `15.0`).
- NotebookLM access via `notebooklm-py` requires **Google session
  cookies**, not an API key. In practice this means an authenticated
  session/profile must be set up out-of-band via the `notebooklm` CLI
  (`notebooklm login`) or by supplying `NOTEBOOKLM_AUTH_JSON` /
  `NOTEBOOKLM_HOME` / `NOTEBOOKLM_PROFILE` env vars — see "NotebookLM
  integration" below. Don't invent an API-key-based auth flow.

## NotebookLM integration (`notebooklm-py`)

Full reference: https://github.com/teng-lin/notebooklm-py/blob/main/docs/python-api.md

Key points an agent should know before touching `util/summarizer.py`:

- The client is **async** and is created as an async context manager by
  `main.py`:
  ```python
  from notebooklm import NotebookLMClient

  async with await NotebookLMClient.from_storage() as client:
      ...
  ```
- It is **not thread-safe** and is only re-entrant on a single event loop —
  do not share a client across threads or multiple event loops. In a
  discord.py bot this generally means creating/reusing one client per bot
  process/event loop (discord.py already runs on a single asyncio loop),
  or opening a short-lived client per operation.
- `NotebookLMSummaryService` in `util/summarizer.py` is the workflow boundary
  used by autosummary. It returns typed `Topic` values and classifies
  transient versus permanent summary failures.
- Typical flow for "summarize a video":
  1. Create a temporary notebook with `client.notebooks.create(...)`.
  2. Attach the normalized video URL with
     `client.sources.add_url(notebook_id, url)`.
  3. Ask `client.chat.ask(...)` for JSON topic names and then topic details;
     parse and persist them as `Topic` values in `VideoSummary`.
  4. Persist autosummary topic details in `SummaryJob` before posting them to
     Discord. Respect Discord's 2,000-character message limit when posting.
- `RPCError` is the library's exception type for API failures (auth
  expiry, rate limiting, bad params) — catch it in the cog layer and
  surface a user-friendly Discord error message rather than letting it
  propagate.
- Long-running bot processes should consider passing `keepalive=<seconds>`
  to `NotebookLMClient.from_storage()` / the constructor to avoid the
  session silently going stale.
- Respect Google's rate limits: avoid firing many notebook/source/chat
  calls in a tight loop; add backoff/delay for bursts (e.g. many Discord
  users summarizing at once).

## discord.py conventions

- Keep cogs focused: one feature area per cog module/folder
  (`cogs/misc`, `cogs/summarizer`, ...). Each cog subclasses
  `commands.Cog` (or uses `app_commands` for slash commands) and is loaded
  via an extension `setup()` function.
- `bot.py` owns the `Bot` instance, intents configuration, dependency
  injection, cog/extension loading, and shutdown. `main.py` owns MongoDB,
  migrations, and the NotebookLM context manager.
- Keep the user-facing slash-command names stable: `/topics`, `/detail`,
  `/addchannel`, `/removechannel`, and `/listchannels`. Python method and
  model names may be made more descriptive without changing those command
  names.
- `Autosummary` claims due jobs atomically before processing them. Keep
  generation and Discord delivery as separate retryable phases, and preserve
  the persisted summary payload across delivery failures.
- Use `util/discord.py`'s `split_message` for Discord output. Every emitted
  chunk must be non-empty and at most 2,000 characters; keep boundary tests
  for long lines, punctuation, and exact-limit content.
- Long NotebookLM operations (source ingestion, artifact generation) can
  take a while — use `await interaction.response.defer()` /
  `ctx.typing()` (as appropriate for slash vs. prefix commands) so Discord
  doesn't time out the interaction while summarization is in progress.

## Code style

- Formatting/linting is enforced by `ruff` (see `[dependency-groups] dev`
  in `pyproject.toml`) and `pre-commit`. Run `uv run ruff check --fix .`
  and `uv run ruff format .` before finishing a change; don't hand-format
  against ruff's rules.
- Follow existing patterns for module layout: Discord-facing code in
  `cogs/`, persistence models in `model/`, and non-Discord business logic
  (NotebookLM calls, data transforms) in `util/`.
- Use type hints throughout — this is a modern (3.12+) async codebase, and
  `notebooklm-py`/`beanie`/`discord.py` are all typed/async-first
  libraries.

## Things to double-check before assuming

Since much of this file is inferred from the directory layout and
`pyproject.toml` rather than the full source, an agent should read the
actual file contents (not just names) before making non-trivial changes,
especially:
- `bot.py` and `main.py` — real startup/config/env-var behavior.
- `model/video.py` — the actual Beanie schema and how it relates to
  Discord messages/users and NotebookLM notebook/source IDs.
- `cogs/summarizer/summarizer.py` vs `util/summarizer.py` vs
  `util/youtube.py` — the exact split of responsibilities currently
  implemented.
- The `tests/` files — read the existing tests first to learn the actual
  mocking/fixture conventions in use (e.g. how a `discord.py` context is
  faked, how the `notebooklm` client is stubbed) before adding new tests,
  so new tests stay consistent with the existing style.
- The `dev` dependency group in `pyproject.toml` — confirm exactly which
  test-related packages (`pytest`, `pytest-asyncio`, `pytest-cov`,
  mocking libs, etc.) are actually pinned before assuming a plugin/marker
  is available.
