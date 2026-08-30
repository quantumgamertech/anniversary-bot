# Legacy Bot PostgreSQL Migration Plan

Legacy Bot supports PostgreSQL when `DATABASE_URL` is set and falls back to SQLite for local development when it is not set.

## Local SQLite Development

Do not set `DATABASE_URL` locally unless intentionally testing PostgreSQL.

```powershell
$env:DATABASE_PATH = "legacy_bot_dev.db"
python main.py
```

If `DATABASE_URL` is empty, the bot uses SQLite through `DATABASE_PATH`, defaulting to `legacy_bot.db`.

## Beta PostgreSQL Configuration

Use a separate Discord application, separate Railway Beta service, and separate Beta PostgreSQL database.

Required Beta variables:

- `DISCORD_TOKEN`: Beta bot token only
- `DATABASE_URL`: Beta PostgreSQL connection string
- `TOPGG_WEBHOOK_AUTH` or `TOPGG_WEBHOOK_SECRET`: Beta-only value if webhooks are tested
- `LEMONSQUEEZY_WEBHOOK_SECRET`: Beta-only value if billing webhooks are tested
- Existing non-secret display/config variables as needed

Never use the production Discord token for Beta testing.

## Legacy Data Compatibility

PostgreSQL initialization is additive only. It creates current tables when missing and preserves existing legacy tables:

- `anniversary_log`
- `guild_milestones`
- `guild_settings`
- `premium_guilds`

Startup does not drop, rename, truncate, or migrate production data.

## Migration Tool

`migrate_sqlite_to_postgres.py` is dry-run only in Phase 1.

```powershell
python migrate_sqlite_to_postgres.py --dry-run --sqlite-path legacy_bot.db --database-url $env:DATABASE_URL
```

The tool reports row counts for current SQLite tables and destination PostgreSQL tables. It redacts the destination connection string and performs no writes.

## Production Cutover Procedure

1. Keep production running on the current approved code until a cutover window is approved.
2. Snapshot/backup Railway PostgreSQL.
3. Read-only copy the live production SQLite database before redeploying.
4. Run the dry-run migration report against the copied SQLite database and a staging PostgreSQL copy.
5. Validate row counts and conflicts.
6. Extend the migration tool from dry-run to explicit write mode only after review.
7. Run migration against staging first.
8. Deploy the PostgreSQL-backed branch only after staging passes Discord, webhook, premium, dashboard, vote, and billing checks.
9. Monitor logs after cutover.

## Rollback Procedure

1. Keep the previous SQLite production commit available.
2. Keep the pre-cutover SQLite database copy.
3. Keep the pre-cutover PostgreSQL snapshot.
4. If cutover fails, redeploy the previous SQLite commit and restore the SQLite database copy to the same runtime path or approved persistent volume.
5. Do not delete PostgreSQL legacy tables during rollback.
