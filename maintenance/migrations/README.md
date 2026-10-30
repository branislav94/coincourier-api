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

Phase 5 shadow-analysis order:

1. Keep `DUPLICATE_SHADOW_ENABLED=false`.
2. Run `006_phase5_duplicate_preflight.sql`. Every result set must be empty.
3. Run `007_phase5_duplicate_shadow.sql`.
4. Verify the `duplicate_assessments` table and its unique pair/policy key.
5. Deploy with the flag still false, then enable it explicitly for shadow data.

Phase 5 does not alter raw identity columns: the deployed runtime already owns
`news_id`, nullable `event_id`, `canonical_url`, and `title_hash`. Normalized
content hashes are computed from bounded candidate source text and only the
pairwise equality signal is persisted. Rollback is therefore just disabling
`DUPLICATE_SHADOW_ENABLED`; retain assessment rows for audit analysis.

Daily publication capacity order:

1. Keep `PUBLISH_DAILY_HARD_CAP_ENABLED=false` and stop all publishers before
   migration/activation work. Preserve existing durable-state flags.
2. Confirm Phase 2 `app-004` is verified, then apply
   `008_publication_daily_capacity.sql` to the application database. Duplicate
   shadow migration `007` is not a prerequisite for this schema.
3. Verify both `publication_capacity_days` and
   `publication_capacity_reservations` are InnoDB and match their column,
   primary-key, publication-key uniqueness, and date-index contracts.
4. Follow [the daily-cap activation procedure](../../docs/GETNEWSAPI_DAILY_PUBLICATION_CAP.md)
   before enabling the flag. Prefer a controlled UTC midnight with every
   producer quiescent. Midday activation requires a verified WordPress baseline
   that includes already-created posts awaiting application completion.

The migration is additive and does not initialize historical capacity. An empty
table is not evidence that the current UTC day has no publications. Schema
readiness does not establish a correct activation baseline. Missing/incompatible
schema must block new CREATEs when enabled. Rollback retains both tables;
disabling enforcement permits additional CREATEs, so stop publishers first.
