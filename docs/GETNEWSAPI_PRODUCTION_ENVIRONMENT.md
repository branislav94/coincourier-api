# GetNewsAPI Production Environment Contract

## Scope

This is the lower-level configuration and packaging reference for canonical
remote DEV and production Compose deployment, migration operations, and external
one-shot jobs. The authoritative procedural guide is
`docs/GETNEWSAPI_DEPLOYMENT_RUNBOOK.md`. The user reports the existing Dockerfile
Application DEV deployment has passed live acceptance. This repository update
does not deploy its Compose replacement or modify that working resource.

## Topology

| File | Purpose | Database ownership |
|---|---|---|
| `docker-compose.dev.yml` | Canonical remote DEV API, service `getnewsapi-dev` | External DEV application DB, WordPress/DB, and MariaDB 11.8 vector DB |
| `docker-compose.yml` | Canonical remote production API, service `getnewsapi-prod` | External production application DB, WordPress/DB, and MariaDB 11.8 vector DB |
| `maintenance/vector/docker-compose.vector.yml` | Local-only vector testing/provisioning | Never a persistent remote DEV or production deployment |

Each remote Compose file defines one API service:

```text
reverse proxy
    |
    +-- external dokploy-network -- getnewsapi-dev:5000 OR getnewsapi-prod:5000
                                      |
                                      +-- external/private application MariaDB
                                      +-- external/private vector MariaDB 11.8
                                      +-- external WordPress DB
                                      +-- HTTPS -- WordPress and approved providers
```

Compose owns the API process lifecycle only. It creates no database, WordPress,
worker, or scheduler service and does not control those resources' persistence.
The API joins the existing external `dokploy-network`; operators must verify the
managed DB endpoints are reachable there. Runtime `DB_*`, `WP_*`, `WP_DB_*`, and
`VECTOR_DB_*` values select the endpoints. No database hostname is fixed in YAML.
Distinct DEV/production service names avoid shared-network alias ambiguity, and
Dokploy/Compose owns physical container naming. Only internal port 5000 is exposed;
no host port or source bind mount is used.

Both files build the repository-root Dockerfile and run `python app.py` in `/app`.
Both fix `APP_ENV=production`, `API_DOCS_ENABLED=false`,
`ENABLE_APSCHEDULER=false`, and `PYTHONUNBUFFERED=1`. Remote DEV intentionally uses
production-safe validation while its endpoints, credentials, and domains remain
DEV. `/ready` probes the vector database only after that feature is enabled.

## Production Environment

Dokploy writes the Compose resource's runtime variables to an uncommitted `.env`
next to the selected Compose file. The service explicitly loads it through
`env_file: ${GETNEWSAPI_RUNTIME_ENV_FILE:-.env}`. Supplying `--env-file` to the
Compose CLI provides interpolation values; it does not itself inject all of those
values into the container. The service `env_file` supplies the complete runtime
environment, with fixed deployment invariants taking precedence.

Only application runtime values belong in that file. Never include
`VECTOR_MARIADB_*` provisioning/root credentials or database root secrets.
`.env.example` is the sanitized runtime inventory; `.env.provisioning.example`
is a separate local vector provisioning reference, not a remote API requirement.
No secret is a build argument, tracked environment file, or hard-coded YAML value.

For static validation, create a temporary file outside tracked paths using only
synthetic values. Set `GETNEWSAPI_RUNTIME_ENV_FILE` in the calling shell to that
file's absolute path, so both interpolation and the service `env_file` select
sanitized data. From the repository root:

```text
docker compose --env-file <sanitized-temp-env> \
  -f docker-compose.dev.yml config --quiet

docker compose --env-file <sanitized-temp-env> \
  -f docker-compose.yml config --quiet
```

Inspect any rendered configuration only with these synthetic values, never real
secrets. Verify one API service, complete runtime injection, no host ports, the
external network, and persistent `/data`. Delete the temporary file and restore
the shell override afterward. Static `config` validation does not start Docker
services, create volumes, or contact providers/DBs. See the [Dokploy Compose environment/storage contract](https://docs.dokploy.com/docs/core/docker-compose).

Blank values in `.env.example` are secrets or environment-specific inputs. From
the repository root, `python GetNewsAPI/tasks.py config_check web` validates the
effective web configuration without opening a network or database connection.
Additional offline profiles are `fetch`, `process`, `pipeline`, `publish`,
`embedding`, `embedding_ingest`, `embedding_worker`, and `embedding_backfill`.

The web profile requires application DB settings and `PUBLISH_API_TOKEN`. Pipeline
and publishing profiles add only the credentials their operations need. Disabled
vector, embedding, semantic, and provider integrations do not become startup
credential requirements.

Process and pipeline profiles require the existing `OPENAI_API_KEY` for factual
enrichment, independently of writer-provider selection. The reviewed defaults
are:

```text
ENRICHMENT_MODEL=gpt-5.6-luna
ENRICHMENT_REASONING_EFFORT=low
ENRICHMENT_SEARCH_CONTEXT_SIZE=low
ENRICHMENT_MAX_OUTPUT_TOKENS=1200
```

Enrichment uses the OpenAI Responses API with hosted `web_search` as its only
tool, `tool_choice="required"`, and `store=False`. It returns bounded factual
context to the existing Grok-primary/OpenAI-fallback writer. Model must be
non-empty, reasoning must be `low`, search context must be `low`, `medium`, or
`high`, and the output-token limit must be 1 through 4096. These settings do not
alter existing image or embedding configuration. Configuration checks are
offline and do not verify provider-account access. See the [official model contract](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
and [hosted web-search documentation](https://developers.openai.com/api/docs/guides/tools-web-search).

WordPress REST calls use the centralized Requests timeout tuple
`WP_HTTP_CONNECT_TIMEOUT_SECONDS=10` and `WP_HTTP_READ_TIMEOUT_SECONDS=60`.
Configuration validation accepts only positive values up to 60 and 300 seconds,
respectively. A mutating read timeout is not immediately retried; the next
durable attempt runs existing post/media reconciliation first.

## Safe Initial State

The image and remote Compose do not run migrations, fetch, process, publish,
embed, or backfill at startup. `ENABLE_APSCHEDULER` is fixed false for the web
service. These source defaults remain false; Compose preserves externally supplied
rollout values instead of forcing a currently accepted DEV deployment back off:

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

Remote DEV uses the same hardened state paths as production. Local source
development keeps `/app/cache` defaults unless paths are overridden. The container
runs as Dockerfile UID/GID 10001:10001, while application source remains root-owned
and read-only. The logical volumes `getnewsapi-dev-state` and
`getnewsapi-prod-state` have Docker Compose project-scoped names, isolating DEV
from production. Keep each Dokploy Compose project identity stable across normal
redeploys. Rebuild/recreate retains the named volume; deleting a volume or using
`down -v` does not.

Named-volume initialization uses the image's owned `/data` directory for a new
empty volume. Operators must verify ownership and writability before acceptance,
especially for reused/restored volumes. Do not guess an existing Application
volume name. Inspect and either safely reuse the confirmed volume through a
reviewed platform mount configuration, or copy `/data` once into the new volume.
Quiesce old writers before a final consistent copy and verify UID/GID 10001:10001
before enabling new jobs. The [Dokploy DEV handoff](GETNEWSAPI_DOKPLOY_DEV_HANDOFF.md)
contains the parallel cutover; this repository task performs no remote copy.

Both services retain init, read-only root, all capabilities dropped,
`no-new-privileges`, restart `unless-stopped`, and `/tmp` tmpfs bounded to 64 MiB
with mode 1777. The Dockerfile's non-root USER remains authoritative. Container
health checks `http://127.0.0.1:5000/health`; use `/ready` for operator readiness,
so transient external DB failure is not a liveness failure.

## Database TLS

Application, vector, and WordPress DB connections support the connector-verified
options `ssl_disabled`, `ssl_ca`, `ssl_verify_cert`, and `ssl_verify_identity` via
the `DB_*`, `VECTOR_DB_*`, and `WP_DB_*` TLS variables in `.env.example`.

Existing-compatible defaults offer TLS without certificate verification. For an
external production DB, enable certificate and hostname verification and mount a
CA file into the container, then set the corresponding `*_SSL_CA` path. The remote
vector DB is separately managed and uses its recorded endpoint TLS policy.
Source defaults for all three DB connections offer TLS with certificate and
identity verification off; Compose does not override that policy. No certificates
are shipped by the image, and HTTPS provider verification is unchanged.

## Vector Resource Ownership

Remote DEV and production consume separately managed MariaDB 11.8 vector
resources. The existing populated DEV vector database is retained; API replacement
does not provision a replacement or modify its migration chain. DevOps owns DB
storage, least-privilege accounts, private routing, backup, and restore. The API
uses only `VECTOR_DB_HOST`, `VECTOR_DB_PORT`, `VECTOR_DB_NAME`, `VECTOR_DB_USER`,
`VECTOR_DB_PASSWORD`, connection timeout, and `VECTOR_DB_SSL_*` settings.

`maintenance/vector/docker-compose.vector.yml` and its provisioning defaults are
LOCAL DEVELOPMENT / VECTOR VALIDATION ONLY, never persistent remote DEV,
production, or shared infrastructure. `VECTOR_MARIADB_ROOT_PASSWORD` and other
provisioning values must never enter remote web or one-shot runtime environments.

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
GETNEWSAPI_RUNTIME_ENV_FILE=/secure/path/getnewsapi-runtime.env \
docker compose --env-file /secure/path/getnewsapi-runtime.env \
  -f docker-compose.yml run --rm getnewsapi-prod \
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

Remote DEV and production have exactly one scheduler owner: Dokploy.
`getnewsapi-dev`/`getnewsapi-prod` serve HTTP only, fix `ENABLE_APSCHEDULER=false`, and
does not start recurring work. Production configuration rejects
`APP_ENV=production` with `ENABLE_APSCHEDULER=true`. Local source development may
retain the in-process APScheduler compatibility path; canonical remote DEV does not.

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
| `embedding_ingest` | externally scheduled DEV | application and vector DB; no embedding provider call | default `EMBEDDING_INGEST_LIMIT=25`, maximum 1000 | vector-DB `embedding-registration` job lock |
| `embedding_worker` | externally scheduled DEV | application/vector DB and configured embedding provider | default `EMBEDDING_WORK_LIMIT=5`, maximum 100 jobs; `EMBEDDING_MAX_CHUNKS_PER_JOB=100` by default | no global job lock; durable row claims support parallel workers |
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

The intended DEV schedule is UTC and configured by the operator as Dokploy
COMPOSE jobs targeting `getnewsapi-dev` in `/app`:

| UTC cron | Command |
|---|---|
| `0,30 * * * *` | `python tasks.py fetch_once` |
| `2,32 * * * *` | `python tasks.py embedding_ingest 25` |
| `3,33 * * * *` | `python tasks.py embedding_worker 5` |
| `5,35 * * * *` | `python tasks.py pipeline_once` |

Create new jobs disabled. Disable the old Application jobs and let active runs
finish before enabling the Compose jobs; never leave both sets enabled. Verify
one complete sequence and its bounded results. Production jobs target
`getnewsapi-prod` only after separate production approval. No schedules, cron,
sidecars, or scheduler services are implemented by these Compose files.
Backfill stays manual. A pipeline run still attempts process then publish;
publication runs when processing finds no work or fails, preserving backlog
draining. Any failed stage makes the overall result nonzero and identifies it.

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
GETNEWSAPI_RUNTIME_ENV_FILE=/secure/path/getnewsapi-runtime.env \
docker compose --env-file /secure/path/getnewsapi-runtime.env \
  -f docker-compose.yml run --rm getnewsapi-prod \
  python tasks.py fetch_once

GETNEWSAPI_RUNTIME_ENV_FILE=/secure/path/getnewsapi-runtime.env \
docker compose --env-file /secure/path/getnewsapi-runtime.env \
  -f docker-compose.yml run --rm getnewsapi-prod \
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

The earlier Phase 7D documentation work did not deploy or activate features.
The user's later live DEV acceptance is separate evidence. This Compose contract
update performs no remote deployment, DB mutation, provider call, scheduler
conversion, or `/data` copy; use the handoff for a separately executed parallel
DEV replacement.
