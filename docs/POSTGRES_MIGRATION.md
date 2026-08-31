# Legacy Bot PostgreSQL Migration Plan

Legacy Bot supports PostgreSQL when `DATABASE_URL` is set and falls back to SQLite for local development when it is not set.

## Local SQLite Development

Do not set `DATABASE_URL` locally unless intentionally testing PostgreSQL.

```powershell
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
$env:DATABASE_PATH = "legacy_bot_dev.db"
python main.py
```

If `DATABASE_URL` is empty, the bot uses SQLite through `DATABASE_PATH`, defaulting to `legacy_bot.db`.

## Disposable Beta PostgreSQL

Use a separate Discord application, separate Railway Beta service, and separate Beta PostgreSQL database.

Required Beta variables:

- `DISCORD_TOKEN`: Beta bot token only
- `DATABASE_URL`: Beta PostgreSQL connection string only
- `TOPGG_WEBHOOK_AUTH` or `TOPGG_WEBHOOK_SECRET`: Beta-only value if webhooks are tested
- `LEMONSQUEEZY_WEBHOOK_SECRET`: Beta-only value if billing webhooks are tested
- Existing non-secret display/config variables as needed

Never use the production Discord token for Beta testing.
Never point Beta at production PostgreSQL.

## Bot Startup Schema Initialization

Normal bot startup initializes schema intentionally through `create_database_from_env(...)`.

That path may create or alter tables in the configured database. Use it only with local SQLite or a disposable/Beta PostgreSQL database until production cutover is approved.

## Dry-Run Audit

Dry-run mode is read-only by design. It does not use the bot `Database` class and does not initialize schema.

```powershell
python migrate_sqlite_to_postgres.py --dry-run --sqlite-path legacy_bot.db --database-url $env:DATABASE_URL
```

Dry-run reports:

- SQLite table existence, columns, and row counts
- PostgreSQL table existence, columns, and row counts
- missing destination tables/columns
- legacy-data mapping plan
- conflicts that require manual review

Dry-run must not run `CREATE`, `ALTER`, `INSERT`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, or `VACUUM`.

## Explicit Schema Init

Schema init is a separate explicit mode.

```powershell
python migrate_sqlite_to_postgres.py --init-schema --database-url $env:DATABASE_URL
```

For production-looking database URLs, the tool refuses write-capable modes unless explicitly overridden:

```powershell
python migrate_sqlite_to_postgres.py --init-schema --database-url $env:DATABASE_URL --allow-production-url
```

Do not use the override against production until the cutover window is approved.

## Apply Mode

Apply mode is intentionally guarded. It requires an explicit confirmation flag and currently remains blocked until a reviewed write plan is approved.

```powershell
python migrate_sqlite_to_postgres.py --apply --sqlite-path legacy_bot.db --database-url $env:DATABASE_URL --yes-i-understand
```

Production-looking database URLs also require:

```powershell
--allow-production-url
```

## Legacy Data Mapping Policy

`premium_guilds.guild_id` maps to `guild_settings.premium = true` only in explicit apply mode.

Legacy `guild_settings.channel_id` can map to the current compatible channel field only when the canonical value is empty. If both SQLite/current Postgres and legacy Postgres have values, the migration reports a conflict and does not silently overwrite.

Legacy `guild_settings.custom_message` is preserved. If a current canonical value exists, the migration reports a conflict.

`guild_milestones` rows are preserved. Role names are not guessed into role IDs. Unresolved role mappings are reported for manual review.

`anniversary_log` is preserved as historical data and must not be discarded.

## Production Cutover Procedure

1. Keep production running on the current approved code until a cutover window is approved.
2. Snapshot/backup Railway PostgreSQL.
3. Read-only copy the live production SQLite database before redeploying.
4. Run dry-run against the copied SQLite database and a staging PostgreSQL copy.
5. Review row counts, missing columns, missing tables, and conflicts.
6. Initialize schema in staging only.
7. Approve and implement write migration behavior after the reviewed conflict report.
8. Run migration against staging first.
9. Test every Discord command, Top.gg webhook, billing webhook, premium gate, dashboard, and background loop in Beta.
10. Snapshot production Postgres again immediately before cutover.
11. Run the approved migration during the cutover window.
12. Deploy the PostgreSQL-backed production code only after migration validation.
13. Monitor logs and command behavior.

## Rollback Procedure

1. Keep the previous SQLite production commit available.
2. Keep the pre-cutover SQLite database copy.
3. Keep the pre-cutover PostgreSQL snapshot.
4. If cutover fails before new writes happen, redeploy the previous SQLite commit and restore the SQLite database copy to the same runtime path or approved persistent volume.
5. If cutover fails after new PostgreSQL writes happen, stop and reconcile the Postgres writes before returning to SQLite.
6. Do not delete PostgreSQL legacy tables during rollback.
