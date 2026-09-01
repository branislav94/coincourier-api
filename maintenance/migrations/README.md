# Manual database migrations

These migrations are versioned, additive, and intentionally never run by the
application. Run them only against the intended application database after a
backup and schema review.

The repository schema dump identifies MariaDB 10.4. These scripts therefore
declare MariaDB 10.4 as their minimum supported version; compatibility with an
older server has not been inferred. MariaDB 10.4 supports the used
`ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` forms.

Phase 2 order:

1. Run `001_phase2_identity_preflight.sql`. Every result set must be empty.
2. Run `002_phase2_durable_state.sql`.
3. Run `003_phase2_uniqueness_preflight.sql`. Every result set must be empty.
4. Run `004_phase2_indexes.sql`.
5. Deploy the code with both flags still false, then explicitly set
   `PROCESS_DURABLE_CLAIMS_ENABLED=true` and
   `PUBLISH_DURABLE_STATE_ENABLED=true` only after migration verification.

The DDL uses MariaDB `IF NOT EXISTS` and every backfill uses `COALESCE` or a
boolean compatibility condition, so an interrupted migration can be rerun.
Index creation is deliberately separate from collision checks.

Rollback does not remove columns or indexes. Disable both feature flags, stop
workers, and run `005_phase2_rollback_state.sql` to release active claims. The
legacy `processed` and `published` booleans remain authoritative to the legacy
path. Retain the additive state for diagnosis and reconciliation.
