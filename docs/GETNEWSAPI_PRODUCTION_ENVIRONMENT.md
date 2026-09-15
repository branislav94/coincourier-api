# GetNewsAPI Production Environment Contract

## Scope

This document describes the Phase 7B package. It is a configuration contract,
not a deployment runbook. No deployment has been performed. Database migration
planning, apply/verify commands, backup gates, and scheduled one-shot job
orchestration remain Phase 7C work.

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
before Phase 7C migration verification is not authorized by this package.

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
behavior. Phase 7C must define the ordered check/apply/verify mechanism before any
schema-dependent feature is enabled.

## Phase 7C Boundary

Before any schema-dependent feature is enabled, Phase 7C must define and verify:

- application and vector migration plan/check/apply/verify commands;
- backup and restore confirmation gates;
- migration ordering and operational deployment services;
- externally scheduled, bounded one-shot fetch/process/publish/embedding jobs;
- single-owner concurrency and activation procedures.

Phase 7B intentionally contains none of those execution paths.
