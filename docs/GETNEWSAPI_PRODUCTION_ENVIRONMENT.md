# GetNewsAPI Production Environment Contract

## Scope

This document describes the Phase 7B package, Phase 7C1 migration operations, and
the Phase 7C2 external one-shot job contract. It remains the lower-level
configuration and packaging reference. The authoritative procedural guide is
`docs/GETNEWSAPI_DEPLOYMENT_RUNBOOK.md`. No deployment has been performed and no
scheduler product has been configured.

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

Keep two production files outside the repository and Docker build context:

- an application runtime file based on `.env.example`;
- a Compose interpolation/provisioning file based on
  `.env.provisioning.example`.

The provisioning file sets `GETNEWSAPI_RUNTIME_ENV_FILE` to the runtime file's
absolute path. Validate from the repository root without rendering resolved
environment values:

```text
docker compose --env-file /secure/path/getnewsapi-provisioning.env \
  -f docker-compose.prod.yml config --quiet
```

`.env.example` is the complete sanitized application-runtime reference. Blank
values in that file are secrets or environment-specific inputs. From the
repository root, `python GetNewsAPI/tasks.py config_check web` validates the
effective web configuration without opening a network or database connection.
Additional offline profiles are `fetch`, `process`, `pipeline`, `publish`,
`embedding`, `embedding_ingest`, `embedding_worker`, and `embedding_backfill`.

The web profile requires application DB settings and `PUBLISH_API_TOKEN`. Pipeline
and publishing profiles add only the credentials their operations need. Disabled
vector, embedding, semantic, and provider integrations do not become startup
credential requirements.

WordPress REST calls use the centralized Requests timeout tuple
`WP_HTTP_CONNECT_TIMEOUT_SECONDS=10` and `WP_HTTP_READ_TIMEOUT_SECONDS=60`.
Configuration validation accepts only positive values up to 60 and 300 seconds,
respectively. A mutating read timeout is not immediately retried; the next
durable attempt runs existing post/media reconciliation first.

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

The protected provisioning file supplies these values only to Compose
interpolation and the vector MariaDB service, with no password defaults:

```text
VECTOR_MARIADB_DATABASE
VECTOR_MARIADB_USER
VECTOR_MARIADB_PASSWORD
VECTOR_MARIADB_ROOT_PASSWORD
```

The separate runtime file supplies `VECTOR_DB_NAME`, `VECTOR_DB_USER`, and
`VECTOR_DB_PASSWORD`; they must identify the same non-root application account
created from the provisioning values. Compose fixes
`VECTOR_DB_HOST=getnewsapi-vector-mariadb` and `VECTOR_DB_PORT=3306` for the web
service. `VECTOR_MARIADB_ROOT_PASSWORD` never enters the web or one-shot job
environment. The named `getnewsapi-vector-data` volume persists database files,
and the vector port is never published to the host.

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
docker compose --env-file /secure/path/getnewsapi-provisioning.env \
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

## External One-Shot Job Contract

Production has exactly one scheduler owner: an external scheduler chosen by
DevOps. `getnewsapi-web` serves HTTP only, fixes `ENABLE_APSCHEDULER=false`, and
does not start recurring work. Production configuration rejects
`APP_ENV=production` with `ENABLE_APSCHEDULER=true`. Development may retain the
in-process APScheduler compatibility path.

The explicit catalog is available without network or database access:

```text
python tasks.py job_catalog
```

The supported jobs are:

| Job | Class | Work and external activity | Bound | Concurrency contract |
|---|---|---|---|---|
| `fetch_once` | recurring | application DB; CryptoNews fetches; OpenAI scoring | at most three 100-item article pulls, optional 50-item video pull, `FETCH_POOL_SIZE`, and `FETCH_SCORE_LIMIT` | existing application-DB `news_fetcher_lock`; overlap skips |
| `pipeline_once` | recurring | process against application DB and text/search providers, then publish against application/WordPress DB, WordPress REST, and image providers | `PROCESS_BATCH_MIN..PROCESS_BATCH_MAX` within `PROCESS_LOOKAHEAD_MINUTES`, then `PUBLISH_BATCH_MAX` | application-DB `pipeline-workflow` job lock; overlap skips |
| `process` | manual | application DB and configured text/search providers | processing bounds above | shares `pipeline-workflow` |
| `publish` | manual | application/WordPress DB, WordPress REST, and configured image providers | `PUBLISH_BATCH_MAX` | shares `pipeline-workflow`, then retains `wp_publisher_lock` |
| `embedding_ingest` | rollout decision | application and vector DB; no embedding provider call | default `EMBEDDING_INGEST_LIMIT=25`, maximum 1000 | vector-DB `embedding-registration` job lock |
| `embedding_worker` | rollout decision | application/vector DB and configured embedding provider | default `EMBEDDING_WORK_LIMIT=5`, maximum 100 jobs; `EMBEDDING_MAX_CHUNKS_PER_JOB=100` by default | no global job lock; durable row claims support parallel workers |
| `embedding_backfill` | manual | application and vector DB; no embedding provider call | default/maximum changed registrations 25/1000; 100-row keyset pages do not impose a total scan ceiling | shares vector-DB `embedding-registration` |

Every command performs one terminating run. Output work is bounded as listed;
historical backfill may scan prior history to find its bounded changed
registrations. No command creates a daemon or recurring scheduler. The
recommended production commands are:

```text
python tasks.py fetch_once
python tasks.py pipeline_once
python tasks.py embedding_ingest [limit]
python tasks.py embedding_worker [limit]
python tasks.py embedding_backfill [source|generated] [limit]
```

`process`, `publish`, and `embedding_backfill` are controlled manual commands,
not independent recurring schedules. The legacy `fetch` and `chained` names remain
compatibility aliases for `fetch_once` and `pipeline_once`.

The current-equivalent baseline is `fetch_once` every 30 minutes and
`pipeline_once` every 30 minutes approximately three minutes after fetch. A
pipeline run always attempts process first and publish second. Publish still runs
when processing finds no work or fails, preserving backlog draining; any failed
stage makes the overall result nonzero and identifies the stage. No embedding
cadence has been approved. Scheduling `embedding_ingest` or `embedding_worker`
remains a rollout decision, while backfill remains manual.

New job locks use stable names of the form
`getnewsapi:job:v1:<conflict-group>:<database-scope-hash>`. They are distinct from
migration locks, use a dedicated MariaDB connection with autocommit enabled, and
wait for `JOB_LOCK_TIMEOUT_SECONDS` (default one second; allowed range 1-60).
There is no open SQL transaction during provider work. A busy conflict group
returns `SKIPPED_ALREADY_RUNNING`; the lock is released in `finally`, and MariaDB
also releases it when the owning connection disconnects.

Structured command output uses `SUCCESS`, `NO_WORK`,
`SKIPPED_ALREADY_RUNNING`, `DISABLED`, `CONFIGURATION_ERROR`, or `FAILED`, with
bounded counts, duration, stage, and reason type. Success, no work, expected
overlap, and a disabled feature exit 0. Configuration, database, provider, job,
or partial-pipeline failure exits 1. Invalid CLI usage exits 2. Output omits
credentials, article bodies, vectors, and provider payloads.

Use the same immutable image, environment file, networks, read-only root, `/tmp`,
and persistent `/data` mounts as the web service. No source mount or special worker
image is required:

```text
docker compose --env-file /secure/path/getnewsapi-provisioning.env \
  -f docker-compose.prod.yml run --rm getnewsapi-web \
  python tasks.py fetch_once

docker compose --env-file /secure/path/getnewsapi-provisioning.env \
  -f docker-compose.prod.yml run --rm getnewsapi-web \
  python tasks.py pipeline_once
```

Compose intentionally defines no permanent worker or scheduler service. One-shot
jobs never run migrations, change feature flags, or enable vector, embedding,
semantic, durable-state, duplicate-shadow, or Image Search V2 behavior.

## Authoritative Deployment Runbook

Phase 7D is implemented locally as
`docs/GETNEWSAPI_DEPLOYMENT_RUNBOOK.md`. That document is authoritative for
environment ownership, DEV and production preflight, backup/restore gates,
migration sequencing, one-shot jobs, staged feature activation, monitoring,
disable/recovery actions, and DEV-to-production promotion. This file remains the
lower-level environment contract.

Phase 7D was documentation-only: it did not deploy, access Dokploy, connect to a
live service, execute a migration, choose an embedding cadence, or activate a
feature. Phase 7E remains the separate clean-room or remote-DEV rehearsal.
