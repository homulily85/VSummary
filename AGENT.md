# AGENT.md

Guidance for AI coding agents (and humans) working on **vsummary** — a Discord
bot that summarizes videos using NotebookLM.

## Project overview

- **Name:** `vsummary`
- **Purpose:** Discord bot that takes video links/content, feeds them into
  NotebookLM, and posts back a summary in Discord.
- **Language / runtime:** Python >= 3.12
- **Package manager / build backend:** `uv` (see `uv.lock`) with
  `setuptools` as the PEP 517 build backend.
- **Entry point:** console script `vsummary` → `vsummary.main:main`
  (see `[project.scripts]` in `pyproject.toml`).

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
│   └── summarizer/
│       └── summarizer.py# Discord-facing commands that trigger video summarization
├── model/
│   └── video.py          # Beanie Document model(s) describing a "video" record
└── util/
    └── summarizer.py      # NotebookLM integration logic (non-Discord-specific helpers)
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
```

There is no test suite in the tree yet. If you add tests, prefer `pytest`
and add it to the `dev` dependency group in `pyproject.toml`, plus a
`[tool.pytest.ini_options]` section if custom config is needed.

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
- `cogs/summarizer/summarizer.py` vs `util/summarizer.py` — the exact
  split of responsibilities currently implemented.