# Upgrading to 1.6

1. Stop **all** bot processes and workers. Keep them stopped throughout dry-run,
   migration, and verification. Do not run versions 1.5 and 1.6 together.
2. Back up the entire `VSummary` database with your normal MongoDB backup tool
   (for example, `mongodump`) and retain the previous application/configuration.
3. Install the 1.6 dependencies with `uv sync`. Set `MONGODB_URI` in the working
   directory's `.env` or environment. The script needs no Discord token and
   never connects to Discord or NotebookLM.
4. Preview the migration (no writes, including no index changes):

   ```bash
   uv run python -m vsummary.migrations.v1_6
   ```

5. Resolve any reported schema, `_id`, or unique-key conflicts. The script
   does not choose between different records or overwrite newer job progress.
   Then apply:

   ```bash
   uv run python -m vsummary.migrations.v1_6 --apply
   ```

6. Require exit code 0 and `Applied and verified`. Review target record counts,
   then run dry-run again: it should report zero writes. Start the 1.6 bot and
   check followed channels and `/queue`.

## Data conversion

- `Channel` → `FollowedChannel`.
- `Video` → `VideoSummary`, keeping `_id`, cached topics and language tags.
  Missing language remains `None` and is excluded from new cache lookups;
  null topics become an empty list.
- `PendingVideo` → `SummaryJob` with `job_type="auto"`, `source="youtube"`;
  legacy status `done` becomes `completed`.
- `ManualSummaryJob` → `SummaryJob` with `job_type="manual"`; its Discord
  `channel_id` moves to `discord_channel_id`. `channel_id` on auto jobs
  remains the YouTube channel ID. Repeated manual requests remain separate.
- Existing canonical collections are also validated/normalized, including
  auto jobs in `SummaryJob` that do not yet have `job_type`.
- Retry counters, errors, audio fallback, generated payloads, leases and delivery
  checkpoints are preserved. Expired leases are reclaimed by the appropriate
  worker; a still-valid lease can delay processing until it expires.
- `AutoSummaryTopicPolicy` is unchanged.

Preflight checks all records and final unique keys before changing data.
The script verifies resulting records and indexes, preserving unrelated indexes.
It only removes obsolete video-only cache uniqueness and unconditional auto-job
uniqueness. Keep the bot stopped if migration fails; after resolving the cause,
rerun the same command. Identical records already copied are skipped. An
interruption may leave partial target data or index changes; rerunning completes
and verifies the migration. Do not start the bot until verification succeeds.

## Retention and rollback

Source collections are retained unchanged. They do not receive new updates from
1.6 and are not automatically deleted. Record counts refer to canonical targets,
which can contain existing data in addition to migrated source records.

Before 1.6 processes new work, restore the full backup and previous version to
roll back consistently. After 1.6 processes new work, source collections alone
are insufficient for rollback: restore a suitable backup or explicitly reconcile
new jobs, cache changes and delivery progress. Rerunning migration against newer,
different target progress fails safely instead of overwriting that progress.

## Disposable MongoDB verification

The optional MongoDB tests use randomly named `vsummary_migration_test_*`
databases and remove them afterwards. Run only against a disposable server:

```bash
MONGODB_TEST_URI=mongodb://127.0.0.1:27028 uv run pytest tests/test_migration_mongo.py
```
