# Separate vector-database migrations

These migrations target only the independent MariaDB 11.8 database named
`coincourier_vectors`. They are not part of `maintenance/migrations/` and must
never be run against the application database.

Apply in order after verifying `SELECT DATABASE()` returns exactly
`coincourier_vectors` in production, or `coincourier_vectors_test` in disposable
integration tests:

1. `001_vector_schema.sql`
2. `002_vector_indexes.sql`
3. Run the application schema verifier before enabling any vector connection.

Both scripts are rerunnable on a compatible schema. `CREATE TABLE IF NOT EXISTS`
does not validate an incompatible pre-existing table, so a successful rerun is
not proof of compatibility. The verifier checks required provenance columns,
`VECTOR(1536)`, and the cosine `VECTOR` index.

The 1536-dimensional physical contract was exercised on MariaDB 11.8.9 with
synthetic vectors. The provider, model, and long-lived dimension decision is
deferred to Phase 6B. A future dimension change requires side-by-side storage
and re-embedding rather than mixing dimensions in this column.
