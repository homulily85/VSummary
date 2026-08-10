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
   feature, add or update the relevant test file under `tests/`
   (`test_cogs.py`, `test_summarizer.py`, `test_youtube.py`, or a new
   `tests/test_<module>.py` for a new module). The test(s) should express
   the desired behavior and are expected to **fail** at this point since
   the implementation doesn't exist/isn't updated yet.
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
  corresponding existing test file (`test_cogs.py`, `test_summarizer.py`,
  `test_youtube.py`, ...) to cover the new/changed behavior, confirm it
  fails against the old implementation, then change the implementation.
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
├── main.py              # process entry point (main()) — bootstraps and runs the bot
├── bot.py               # discord.py Bot/Client subclass, cog loading, lifecycle
├── cogs/                # discord.py cogs (feature modules), auto/explicitly loaded by bot.py
│   ├── misc/
│   │   └── ping.py      # simple health-check / latency command
│   ├── summarizer/
│   │   └── summarizer.py# Discord-facing commands that trigger video summarization
│   └── autosummary/
│       └── autosummary.py # follow channels and auto-post summaries (Holodex polling)
├── model/
│   ├── video.py          # Beanie Document model(s) describing a "video" record
│   └── channel.py        # Channel + PendingVideo document models for auto-summaries
└── util/
    ├── holodex.py        # Holodex API client + transcript-ready/backoff helpers
    ├── summarizer.py     # NotebookLM integration logic (non-Discord-specific helpers)
    └── youtube.py        # YouTube URL/ID parsing & metadata helpers

tests/
├── test_autosummary.py   # tests for the autosummary cog (commands + polling + retries)
├── test_cogs.py          # tests for the Discord-facing cog layer
├── test_holodex.py       # tests for util/holodex.py
├── test_models.py        # tests for model/channel.py document models
├── test_summarizer.py    # tests for util/summarizer.py (NotebookLM logic)
└── test_youtube.py       # tests for util/youtube.py
```

Notes for agents:
- There are **two files named `summarizer.py`** with different roles:
  `cogs/summarizer/summarizer.py` (Discord command/cog layer — parses user
  input, replies in Discord) and `util/summarizer.py` (business logic layer —
  talks to NotebookLM, has no Discord dependency). Keep this separation:
  cogs should stay thin and delegate to `util/`.
- `__pycache__` directories and `*.egg-info` are build artifacts — never
  edit or commit meaningful changes there; they're regenerated automatically.
- New Discord features should be added as new cogs under `src/vsummary/cogs/`
  and registered/loaded in `bot.py`.
- Data models backed by MongoDB go in `src/vsummary/model/` as Beanie
  `Document` subclasses, and must be registered with
  `beanie.init_beanie(...)` during bot startup (likely in `bot.py` or
  `main.py`).
- `util/youtube.py` holds YouTube-specific helpers (e.g. URL parsing/
  validation, video ID extraction, maybe metadata lookups) used by
  `util/summarizer.py` and/or the summarizer cog. Keep it free of Discord
  and NotebookLM-client dependencies so it stays easily unit-testable.
- `tests/` mirrors the modules it covers on a roughly 1:1 basis
  (`test_cogs.py` ↔ `cogs/`, `test_summarizer.py` ↔ `util/summarizer.py`,
  `test_youtube.py` ↔ `util/youtube.py`). When you add a new module or
  cog, add a matching `tests/test_<module>.py` following this convention
  — see "Testing policy (mandatory)" below.

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
uv run pytest tests/test_summarizer.py

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

- Config/secrets are loaded via `python-dotenv`, so a local `.env` file
  (untracked) is expected. Likely required variables include a Discord bot
  token and MongoDB connection string; check `main.py`/`bot.py` for the
  exact env var names before assuming any, and add new required variables
  there plus in a `.env.example` if one exists or should be created.
- NotebookLM access via `notebooklm-py` requires **Google session
  cookies**, not an API key. In practice this means an authenticated
  session/profile must be set up out-of-band via the `notebooklm` CLI
  (`notebooklm login`) or by supplying `NOTEBOOKLM_AUTH_JSON` /
  `NOTEBOOKLM_HOME` / `NOTEBOOKLM_PROFILE` env vars — see "NotebookLM
  integration" below. Don't invent an API-key-based auth flow.

## NotebookLM integration (`notebooklm-py`)

Full reference: https://github.com/teng-lin/notebooklm-py/blob/main/docs/python-api.md

Key points an agent should know before touching `util/summarizer.py`:

- The client is **async** and must be used as an async context manager:
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
- Typical flow for "summarize a video":
  1. `client.notebooks.create(title)` (or reuse an existing notebook).
  2. `client.sources.add_youtube(notebook_id, url)` (or `add_url` /
     `add_file` depending on what the user supplies).
  3. Either:
     - `client.chat.ask(notebook_id, "Summarize this video")` for a quick
       text answer (`AskResult.answer`), or
     - `client.artifacts.generate_report(notebook_id, report_format=...)`
       followed by `client.artifacts.wait_for_completion(...)` and
       `client.artifacts.download_report(...)` for a fuller
       Markdown report/artifact.
  4. Post the result back to Discord (respect Discord's 2000-character
     message limit — chunk long summaries or use embeds/files).
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
- `bot.py` owns the `Bot`/`Client` instance, intents configuration, and
  cog/extension loading — new cogs should be registered there, not
  instantiated ad hoc elsewhere.
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