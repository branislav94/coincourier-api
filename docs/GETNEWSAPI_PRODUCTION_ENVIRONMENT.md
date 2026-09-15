# GetNewsAPI Production Environment Contract

## Scope

This document describes the Phase 7B package and preliminary Phase 7C1 migration
operations. It is a configuration and command contract, not the final deployment
runbook. No deployment has been performed. External scheduling and recurring job
ownership remain Phase 7C2 work.

## Topology

`docker-compose.prod.yml` defines exactly two services:

```text
reverse proxy
    |
    +-- getnewsapi-edge -- getnewsapi-web:5000
                              |
                              +-- external/private application MariaDB
                              +-- getnewsapi-private -- vector MariaDB 11.8
                              +-- HTTPS -- external providers and WordPress
```

The application MariaDB and WordPress database remain external. Compose creates
only the private vector MariaDB. The web service joins an edge network and an
internal backend network; the vector database joins only the internal network.
Neither service publishes a host port. The reverse proxy must join the generated
`getnewsapi-edge` network and route to `getnewsapi-web:5000`.

The web service does not declare vector database startup as an unconditional
dependency because `VECTOR_ENABLED` defaults to false. `/ready` checks the vector
database only after that feature is explicitly enabled.

## Production Environment

Keep the real production environment file outside the repository and Docker build
context. Supply its path explicitly, for example:

```text
GETNEWSAPI_ENV_FILE=/secure/path/getnewsapi.env docker compose \
  --env-file /secure/path/getnewsapi.env \
  -f docker-compose.prod.yml config
```

`.env.example` is the complete sanitized reference. Blank values in that file are
secrets or environment-specific inputs. `python tasks.py config_check web` validates
the effective web configuration without opening a network or database connection.
Additional offline profiles are `pipeline`, `publish`, and `embedding`.

The web profile requires application DB settings and `PUBLISH_API_TOKEN`. Pipeline
and publishing profiles add only the credentials their operations need. Disabled
vector, embedding, semantic, and provider integrations do not become startup
credential requirements.

## Safe Initial State

The image and production Compose do not run migrations, fetch, process, publish,
embed, or backfill at startup. `ENABLE_APSCHEDULER` is fixed false for the web
service. These source and Compose defaults remain false:

```text
ENABLE_APSCHEDULER
PROCESS_DURABLE_CLAIMS_ENABLED
PUBLISH_DURABLE_STATE_ENABLED
DUPLICATE_SHADOW_ENABLED
VECTOR_ENABLED
EMBEDDING_ENABLED
SEMANTIC_SHADOW_ENABLED
```

`IMAGE_SEARCH_ENGINE` remains `v1` by default. Enabling schema-dependent flags
before successful Phase 7C1 migration verification and an explicit rollout decision
is not authorized by this package.

## API Contract

- `GET /health` is unauthenticated process liveness and performs no I/O.
- `GET /ready` validates core configuration and performs read-only `SELECT 1`
  probes against the application DB and, only when enabled, the vector DB. It does
  not check providers, WordPress REST, or migration/schema state.
- `POST /api/publish` always requires `Authorization: Bearer <PUBLISH_API_TOKEN>`.
  A missing server token makes the route unavailable; an absent or incorrect
  caller token is rejected. Token values are never logged.
- `GET /api/news` retains its existing unauthenticated read-only behavior. Restrict
  it at the reverse proxy if the response is not intended to be public.
- `/docs`, `/redoc`, and `/openapi.json` are enabled by default in development and
  disabled by default in `APP_ENV=production`. `API_DOCS_ENABLED` is the explicit
  override, and production Compose defaults it to false.

## Logging And Writable State

Container stderr/stdout is the canonical log destination. `LOG_LEVEL` defaults to
`INFO`. File logging is retained only as an opt-in compatibility mode through
`FILE_LOGGING_ENABLED=true` and `FILE_LOG_PATH`; production defaults it off.

The production root filesystem is read-only. `/tmp` is a bounded tmpfs used for
temporary media work. A named volume is mounted at `/data` for:

- `/data/cache/stock_images`: disposable HTTP/image response cache;
- `/data/stock_image_usage.json`: behaviorally important image reuse/licensing
  history that must survive container replacement;
- `/data/logs`: used only when file logging is explicitly enabled.

Development keeps the previous `/app/cache` defaults unless paths are overridden.
The container runs as UID/GID 10001 and owns `/data`, while application source
remains root-owned and read-only.

## Database TLS

Application, vector, and WordPress DB connections support the connector-verified
options `ssl_disabled`, `ssl_ca`, `ssl_verify_cert`, and `ssl_verify_identity` via
the `DB_*`, `VECTOR_DB_*`, and `WP_DB_*` TLS variables in `.env.example`.

Existing-compatible defaults offer TLS without certificate verification. For an
external production DB, enable certificate and hostname verification and mount a
CA file into the container, then set the corresponding `*_SSL_CA` path. The
Compose-managed vector DB uses an internal Docker network and defaults its client
TLS off unless DevOps configures certificates on that database. No certificates
are shipped by the image, and HTTPS provider verification is unchanged.

## Vector Provisioning

Compose requires all provisioning values and supplies no password defaults:

```text
VECTOR_MARIADB_DATABASE
VECTOR_MARIADB_USER
VECTOR_MARIADB_PASSWORD
VECTOR_MARIADB_ROOT_PASSWORD
```

It maps database/name/user/password into the corresponding `VECTOR_DB_*`
application settings and fixes `VECTOR_DB_HOST=getnewsapi-vector-mariadb` and
`VECTOR_DB_PORT=3306`. The named `getnewsapi-vector-data` volume persists database
files. The vector port is never published to the host.

## Migration Resources

The immutable application image includes the reviewed operational SQL under
`/app/maintenance/migrations`, `/app/maintenance/sql`, and
`/app/maintenance/vector_migrations`. Test-only database fixtures under
`maintenance/testing` are not packaged. Including these files does not execute
them: the image has no migration entrypoint, startup hook, or automatic apply
behavior. The repository-owned manifests pin every SQL resource by ID, target,
kind, dependency, order, and SHA-256 checksum.

The application forward chain is `001` preflight, `002`, `003` preflight, `004`,
`006` preflight, and `007`. Migration `005` is a claimed-state recovery utility;
it is never part of normal apply. Both `maintenance/sql/fresh_start_*` utilities
and the test-only baseline are also excluded. The vector forward chain is exactly
`001`, `002`, `003` and is guarded for the separate MariaDB 11.8 vector database.

## Migration Commands

Use the same immutable application image for one-shot operations. No source or SQL
bind mount and no special migration image are required:

```text
docker compose --env-file /secure/path/getnewsapi.env \
  -f docker-compose.prod.yml run --rm getnewsapi-web \
  python tasks.py migration_plan app
```

Replace the final arguments with any of:

```text
python tasks.py migration_plan [app|vector|all]
python tasks.py migration_check [app|vector|all]
python tasks.py migration_verify [app|vector|all]
python tasks.py migration_apply [app|vector|all] \
  --backup-confirmed --restore-tested
python tasks.py feature_readiness
```

`migration_plan`, `migration_check`, `migration_verify`, and `feature_readiness`
perform read-only database inspection. Only `migration_apply` mutates. A production
apply refuses to connect until both CLI switches are present. These switches are
operator attestations that an off-server backup exists and restore capability was
tested; GetNewsAPI does not technically verify either assertion.

Local integration uses `APP_ENV=test`, `MIGRATION_TEST_MODE=true`, an explicitly
named `*_test` database, and `--allow-disposable`. This mode is not accepted as a
substitute for production attestations. `MIGRATION_LOCK_TIMEOUT_SECONDS` controls a
bounded target-specific MariaDB advisory lock and defaults to five seconds. App and
vector locks are distinct.

The migration ledger is stored in each target database as
`getnewsapi_migration_ledger`. A row is written only after a script executes and its
specific artifacts verify. Checksums must continue to match the immutable manifest.
Schema artifacts without ledger rows are reported as `untracked_applied` or
`ambiguous` and block apply; there is no automatic baseline/adoption operation.
Likewise, a ledger row cannot substitute for columns, indexes, constraints, foreign
keys, native vector support, or the semantic assessment schema.

Apply is deterministic and fail-fast. MariaDB DDL can commit implicitly, so a
multi-DDL migration is not represented as transactionally atomic. On SQL or
verification failure, later migrations stop and no automatic recovery/rollback or
fresh-start utility runs. Operators must investigate partial effects. Successful
outcomes distinguish `APPLIED_AND_VERIFIED` from the nonzero
`APPLY_SUCCEEDED_VERIFY_FAILED`; a repeated successful apply reports
`NO_PENDING_MIGRATIONS` without duplicate mutation.

`feature_readiness` reports prerequisites for durable processing, durable
publishing, duplicate shadow, vector, embedding, and semantic shadow. It neither
contacts providers nor changes any feature flag.

## Phase 7C2 Boundary

Phase 7C2 still owns external cron/Dokploy schedules, bounded one-shot recurring
fetch/process/publish/embedding wiring, job concurrency, and rollout activation.
Phase 7C1 adds none of those behaviors. Web startup remains non-mutating and does
not run migration commands automatically.
