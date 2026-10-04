# GetNewsAPI Deployment Runbook

## Authority And Scope

This is the authoritative procedural guide for deploying, validating, operating,
and disabling GetNewsAPI. The lower-level environment and packaging contract is
[GETNEWSAPI_PRODUCTION_ENVIRONMENT.md](GETNEWSAPI_PRODUCTION_ENVIRONMENT.md).
Use `.env.example` as the sanitized configuration inventory and the code in
`GetNewsAPI/tasks.py` as the command authority.

This runbook covers local validation, remote DEV, production preflight, migration,
staged activation, one-shot jobs, historical vector backfill, monitoring, and
recovery. It does not authorize a deployment, a database mutation, provider use,
or feature activation. Phase 7E performs the first clean-room or remote-DEV
rehearsal.

Record the Git commit, image tag, image digest, operator, environment, date, and
change approval in the deployment record before executing any mutating step.
Never substitute one environment's migration ledger or backup evidence for
another environment's evidence.

For initial Dokploy DEV resource setup, use the focused
[GETNEWSAPI_DOKPLOY_DEV_HANDOFF.md](GETNEWSAPI_DOKPLOY_DEV_HANDOFF.md).

## Non-Negotiable Safety Rules

- Start every new environment with offline configuration validation and read-only
  migration inspection. Never start with `migration_apply`.
- Keep all rollout flags false until the corresponding schema and preceding stage
  have been verified.
- Never commit an environment file, bake secrets into an image, print a secret or
  full secret DSN, or pass secrets in command arguments that will be retained in
  shell history.
- Never manually create GetNewsAPI-managed application columns, indexes, ledgers,
  duplicate tables, vector tables, or vector indexes. The migration tooling owns
  those artifacts.
- The application migration chain is additive and assumes the existing legacy
  `cryptonewsapi` and `rich_crpytonews` base tables. If a new application database
  lacks them, stop and request a separately reviewed bootstrap procedure from the
  application team. Do not use the legacy SQL dump or fresh-start utilities.
- Treat `untracked_applied`, `ambiguous`, and `drift` as stop conditions. There is
  no automatic adoption, baseline, schema-down, or general rollback operation.
- Keep `ENABLE_APSCHEDULER=false` whenever an external scheduler can invoke jobs.
- Keep `IMAGE_SEARCH_ENGINE=v1` unless a separate reviewed test explicitly
  authorizes V2.

## Environment Types

| Environment | Execution | Services | Intended use |
|---|---|---|---|
| Local development | Source checkout, development Compose, or local image | Local/test dependencies | Unit and disposable integration work |
| Local code against remote DEV | Latest source or immutable candidate image on a developer machine | Remote DEV app DB, vector DB, WordPress, and explicitly approved provider credentials | Primary rapid end-to-end validation topology |
| Remote DEV deployment | The same immutable candidate image on DevOps infrastructure | Remote DEV services and external scheduler | Deployment, restart, network, persistence, and scheduler acceptance |
| Production | The exact DEV-tested image digest | Independently inspected production services | Staged release after a separate production preflight |

The supported high-value DEV topology is:

```text
Developer machine: latest GetNewsAPI source or immutable image
    |
    +--> remote DEV application MariaDB
    +--> remote DEV vector MariaDB 11.8
    +--> remote DEV WordPress REST and WordPress DB
    +--> explicitly configured sandbox/real providers and image/source hosts
```

The developer must have explicit VPN, tunnel, routing, DNS, firewall, and CA
access. Keep APScheduler off. This topology shortens the edit/validate loop while
exercising shared DEV state; it is not disposable, so its migrations use the
persistent-environment safety gate described below.

## Ownership Matrix

| Area | Application team | DevOps | Shared |
|---|---|---|---|
| Code and artifact | Dockerfile, application code, production Compose contract | Registry and artifact availability | Exact commit/image/digest freeze |
| Schema | Migration manifests, checksums, tooling, verification, repair design | DB instance/database/user provisioning and connectivity | Preflight, migration approval, backup/restore gate |
| Runtime | Job commands, feature behavior, rollout sequence, application troubleshooting | Host/VM, Docker or Dokploy, secrets injection, persistent storage | Staged activation and rollback decision |
| Network | Required endpoint/TLS contract | DNS, reverse proxy, TLS, firewall, VPN, private routing | Connectivity acceptance |
| Operations | Safe command and lock semantics | Scheduler product, log/metric platform, backup automation | Monitoring and incident response |

DevOps provisions databases and credentials, but does not write application or
vector tables manually. The application team does not manage hosts, DNS, secret
stores, firewall policy, or backup infrastructure.

## DevOps Input Checklist

Do not place values in a ticket or deployment log. Supply them through an approved
secret channel.

### Application Database

- [ ] DEV/production `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD`
- [ ] MariaDB TLS requirement and `DB_SSL_ENABLED` policy
- [ ] CA file mounted at the configured `DB_SSL_CA` path when verification is on
- [ ] `DB_SSL_VERIFY_CERT` and `DB_SSL_VERIFY_IDENTITY` decision
- [ ] VPN, tunnel, private routing, DNS, and firewall requirements
- [ ] Existing legacy base tables confirmed; no manual GetNewsAPI migration work
- [ ] Persistent storage, off-server backup, retention, and restore capability

### WordPress

- [ ] DEV/production `WP_API_URL`, `WP_USERNAME`, and `WP_APP_PASSWORD`
- [ ] `WP_HTTP_CONNECT_TIMEOUT_SECONDS` and `WP_HTTP_READ_TIMEOUT_SECONDS`
  reviewed; defaults are 10 and 60 seconds
- [ ] `WP_DB_HOST`, `WP_DB_PORT`, `WP_DB_NAME`, `WP_DB_USER`, and `WP_DB_PASSWORD`
- [ ] WordPress DB TLS/CA/verification policy through `WP_DB_SSL_*`
- [ ] REST HTTPS trust, network routes, and DB firewall access
- [ ] Account permissions and environment isolation reviewed

### Vector Database

- [ ] MariaDB 11.8 with native vector capability
- [ ] `VECTOR_DB_HOST`, `VECTOR_DB_PORT`, `VECTOR_DB_NAME`, `VECTOR_DB_USER`, and
  `VECTOR_DB_PASSWORD`
- [ ] Separate `VECTOR_MARIADB_DATABASE`, `VECTOR_MARIADB_USER`,
  `VECTOR_MARIADB_PASSWORD`, and `VECTOR_MARIADB_ROOT_PASSWORD` supplied only to
  Compose provisioning; root credentials are absent from application runtime
- [ ] Database name is `coincourier_vectors`, or a guarded disposable name that
  starts with `coincourier_vectors_` and ends with `_test`
- [ ] TLS/CA/verification policy through `VECTOR_DB_SSL_*`
- [ ] Private network only; no public DB port
- [ ] Persistent storage, capacity monitoring, backup, retention, and restore plan
- [ ] Empty database is acceptable; no vector tables are created manually

### Providers And Images

- [ ] `CRYPTO_NEWS_TOKEN`
- [ ] `GROK_API_KEY` or its compatibility alias `XAI_API_KEY`
- [ ] `OPENAI_API_KEY`
- [ ] `GOOGLE_API_KEY`
- [ ] Credentials for each enabled stock/image provider, including Pexels,
  Pixabay, or Openverse where selected
- [ ] Written decision whether DEV uses isolated DEV credentials or approved
  production credentials, including spend/rate limits
- [ ] `PRIMARY_LLM_PROVIDER`, fallback, model, and image routing values reviewed
- [ ] `IMAGE_SEARCH_ENGINE=v1` retained unless V2 has separate approval

### Infrastructure

- [ ] Host/VM or container platform and immutable registry access
- [ ] Reverse proxy, DNS, certificate, and TLS termination
- [ ] Private DB routing plus required provider/WordPress egress
- [ ] Persistent `/data` and vector storage
- [ ] Backup and restore infrastructure with named owners
- [ ] One external scheduler product and service identity
- [ ] Canonical stdout/stderr collection and alerting destination
- [ ] Deployment, migration, rollback, and incident owners

## Secrets And Local Environment Files

Run every checkout command in this runbook from the repository root. Direct
source task commands use `python GetNewsAPI/tasks.py ...`; the web process uses
`python GetNewsAPI/app.py`. The runtime calls `load_dotenv()` with the standard
filename and finds the ignored configuration at `GetNewsAPI/.env`. Do not invent
`.env.dev.local`; the current runtime does not select it automatically. If
`GetNewsAPI/.env` already exists, do not overwrite it. Use `.env.example` only as
the sanitized application-runtime reference and populate secrets through the
approved secret workflow.

The legacy development `docker-compose.dev.yml` reads the ignored repository-root
`.env`, bind-mounts source, and requires the existing external `dokploy-network`.
The legacy `docker-compose.yml` retains production environment settings without a
source mount or persistent `/data`; neither legacy file is the supported immutable
deployment shape. Use the hardened `docker-compose.prod.yml` contract for remote
DEV and production, with platform environment/secret injection or an external file
outside the repository and Docker build context. Containers started directly from
the candidate image can receive the ignored local file with
`--env-file GetNewsAPI/.env`.

Never commit either local environment file. `.dockerignore` excludes `.env` and
`.env.*` files recursively. Do not print passwords, API keys, WordPress application
passwords, bearer tokens, certificates, or complete secret DSNs.

Begin with these values:

```text
ENABLE_APSCHEDULER=false
PROCESS_DURABLE_CLAIMS_ENABLED=false
PUBLISH_DURABLE_STATE_ENABLED=false
DUPLICATE_SHADOW_ENABLED=false
VECTOR_ENABLED=false
EMBEDDING_ENABLED=false
SEMANTIC_SHADOW_ENABLED=false
IMAGE_SEARCH_ENGINE=v1
RUN_GROK_TEXT_SMOKE=false
RUN_GROK_IMAGE_SMOKE=false
```

## Artifact Build And Execution

Build once from the selected commit. Use a registry-qualified immutable tag in a
shared environment and record the resulting digest:

```text
git rev-parse HEAD
docker build --tag getnewsapi:<git-sha> .
docker image inspect getnewsapi:<git-sha> --format '{{json .RepoDigests}}'
```

A local source process against remote DEV uses the local environment file:

```text
python GetNewsAPI/tasks.py config_check web
python GetNewsAPI/app.py
```

An immutable local container against the same remote services uses:

```text
docker run --rm --env-file GetNewsAPI/.env \
  -p 127.0.0.1:5001:5000 getnewsapi:<git-sha>
```

Remote DEV uses the same image, command, and `/data` contract. DevOps changes only
environment-specific endpoints, secrets, networks, storage, and proxy attachment.
Production Compose uses two external files: an application runtime file based on
`.env.example`, and a Compose interpolation/provisioning file based on
`.env.provisioning.example`. Only the runtime file enters `getnewsapi-web`; the
provisioning file points to it through `GETNEWSAPI_RUNTIME_ENV_FILE` and supplies
the vector MariaDB service credentials. From the repository root:

```text
docker compose --env-file /secure/path/getnewsapi-provisioning.env \
  -f docker-compose.prod.yml config --quiet

docker compose --env-file /secure/path/getnewsapi-provisioning.env \
  -f docker-compose.prod.yml up -d --no-build
```

The protected provisioning file must set the immutable `GETNEWSAPI_IMAGE`,
`GETNEWSAPI_RUNTIME_ENV_FILE=/secure/path/getnewsapi-runtime.env`, optional edge
network, and `VECTOR_MARIADB_*` values. Never pass the provisioning file as a
container `env_file`, and never place `VECTOR_MARIADB_ROOT_PASSWORD` in the
runtime file.

The unmodified production Compose contract fixes `VECTOR_DB_HOST` to its bundled
`getnewsapi-vector-mariadb` service. A DevOps deployment using an external remote
vector database must deploy the same image through the platform's service
definition, or a separately reviewed Compose override, and inject `VECTOR_DB_*`.
Do not edit application source for an environment.

Run one-shot commands from the immutable image with the same effective
configuration. For the production Compose topology:

```text
docker compose --env-file /secure/path/getnewsapi-provisioning.env \
  -f docker-compose.prod.yml run --rm getnewsapi-web \
  python tasks.py job_catalog
```

For a stand-alone candidate image:

```text
docker run --rm --env-file GetNewsAPI/.env \
  getnewsapi:<git-sha> python tasks.py job_catalog
```

## Vector Database Setup

### Case A: Remote DEV Vector MariaDB 11.8

This is preferred. DevOps provisions a private, persistent MariaDB 11.8 instance,
database, least-privilege runtime/migration user, network policy, credentials, and
backup coverage. DevOps does not create vector tables or indexes. Configure the
five `VECTOR_DB_*` connection values and TLS settings locally and in remote DEV.

GetNewsAPI owns the schema workflow:

```text
python GetNewsAPI/tasks.py migration_plan vector
python GetNewsAPI/tasks.py migration_check vector
python GetNewsAPI/tasks.py migration_verify vector
python GetNewsAPI/tasks.py migration_apply vector --backup-confirmed --restore-tested
python GetNewsAPI/tasks.py migration_verify vector
```

Run the first three commands before the apply command. A persistent remote DEV
database must use `APP_ENV=production` for the migration invocation so the tooling
requires the same backup/restore attestations as production. This is a migration
safety mode, not a claim that DEV is production. Do not use `--allow-disposable`
for retained remote DEV.

After remote DEV becomes available, repeat migration inspection, apply approval,
schema verification, small embedding runs, historical sample backfill, retrieval
validation, and semantic-shadow validation there even if local vector validation
already passed.

### Case B: Temporary Local MariaDB 11.8

The repository fallback binds only `127.0.0.1:13309` and persists a named local
volume:

```text
docker compose -f maintenance/vector/docker-compose.vector.yml up -d
docker compose -f maintenance/vector/docker-compose.vector.yml ps
docker compose -f maintenance/vector/docker-compose.vector.yml down
```

For direct source commands, set `VECTOR_DB_HOST=127.0.0.1`,
`VECTOR_DB_PORT=13309`, matching name/user/password values, and
`VECTOR_DB_SSL_ENABLED=false` in the ignored local environment. The default
retained database is `coincourier_vectors`; applying to it requires the
persistent-environment gate (`APP_ENV=production`, backup and restore
attestations).

For an explicitly disposable test database, set the Compose provisioning name to
`coincourier_vectors_local_test`, use the same app-side `VECTOR_DB_NAME`, set
`APP_ENV=test` and `MIGRATION_TEST_MODE=true`, and apply only with:

```text
python GetNewsAPI/tasks.py migration_apply vector --allow-disposable
```

Non-production apply additionally refuses any connected database name that does
not end in `_test`. Only for an intentionally disposable local instance, remove
the container and volume with:

```text
docker compose -f maintenance/vector/docker-compose.vector.yml down -v
```

That command destroys local vector data. Never use it for retained validation
data. Local fallback success does not replace remote DEV verification.

## First Contact: Read-Only Preflight

The first contact with each real DEV or production environment is read-only.
From the repository root, or through the equivalent immutable-image wrapper, run:

```text
python GetNewsAPI/tasks.py config_check web
python GetNewsAPI/tasks.py config_check pipeline

python GetNewsAPI/tasks.py migration_plan app
python GetNewsAPI/tasks.py migration_check app
python GetNewsAPI/tasks.py migration_verify app

python GetNewsAPI/tasks.py migration_plan vector
python GetNewsAPI/tasks.py migration_check vector
python GetNewsAPI/tasks.py migration_verify vector

python GetNewsAPI/tasks.py feature_readiness
```

`config_check` is offline. The migration inspection and readiness commands open
read-only DB connections; they do not call providers or WordPress and do not
activate features. A nonzero `migration_verify` or `feature_readiness` result can
be expected before initial migration. Preserve complete non-secret output in the
change record.

Interpret migration state as follows:

| State/outcome | Meaning | Action |
|---|---|---|
| `pending` | No ledger row and no managed artifact exists | Eligible for reviewed apply after gates pass |
| `applied` | Ledger checksum and full schema artifacts agree | No mutation required |
| `drift` | Ledger, checksum, or verified schema disagrees | Stop; investigate immutable artifact and schema history |
| `untracked_applied` | Complete artifacts exist without a ledger row | Stop; do not auto-adopt or baseline |
| `ambiguous` | Partial artifacts exist without trustworthy ledger history | Stop; inspect partial/environment-specific history |
| `blocked` | The operation refused due to state, target, lock, or safety issues | Resolve the reported cause; do not bypass it |

An older CoinCourier database may legitimately predate
`getnewsapi_migration_ledger`. Do not invent an adoption procedure before its real
schema is inspected and a separate reconciliation plan is reviewed.

## Backup And Restore Gate

Before mutating a persistent DEV or production target:

1. Identify the exact database and migration target.
2. Create a fresh off-server backup or approved snapshot.
3. Record backup identifier, timestamp, owner, retention, and encryption status.
4. Confirm a restore has been tested using the environment's approved procedure.
5. Confirm recovery time, point objectives, available capacity, and rollback owner.
6. Pass both `--backup-confirmed` and `--restore-tested` only after those facts are
   true.

The switches are operator attestations. The application does not create, inspect,
or restore backups. An empty new vector database has little user data to lose, but
its durable service and future corpus still need backup ownership; the tooling
does not waive the persistent-environment gate because the database is empty.

## Application Migration Procedure

Prerequisites: the correct immutable image, existing legacy base tables, read-only
preflight with no blocked state, a fresh backup, tested restore, approval, and all
schema-dependent feature flags off.

The exact application execution sequence is:

| ID | Purpose | Apply behavior |
|---|---|---|
| `app-001` | Phase 2 identity preflight | Read-only preflight for `app-002` |
| `app-002` | Durable process/publish state and backfill | Forward DDL/data migration |
| `app-003` | Uniqueness preflight | Read-only preflight for `app-004` |
| `app-004` | Durable-state indexes | Forward DDL |
| `app-006` | Duplicate-table ownership preflight | Read-only preflight for `app-007` |
| `app-007` | Deterministic duplicate-shadow schema | Forward DDL |

Execute:

```text
python GetNewsAPI/tasks.py migration_plan app
python GetNewsAPI/tasks.py migration_check app
python GetNewsAPI/tasks.py migration_apply app --backup-confirmed --restore-tested
python GetNewsAPI/tasks.py migration_verify app
python GetNewsAPI/tasks.py migration_apply app --backup-confirmed --restore-tested
```

The second apply must report `NO_PENDING_MIGRATIONS` and perform no duplicate
mutation. A first successful apply reports `APPLIED_AND_VERIFIED`. An
`APPLY_SUCCEEDED_VERIFY_FAILED` or `APPLY_FAILED_PARTIAL_POSSIBLE` result is a
failure: stop because MariaDB DDL may have committed partial effects.

Application and vector applies use separate target-specific MariaDB advisory
locks. `MIGRATION_LOCK_TIMEOUT_SECONDS` defaults to five seconds. Lock contention
blocks the operation; do not launch a competing apply.

Never include these in a normal environment migration:

- `app-005`: claimed-state release/recovery utility only, not schema rollback
- `maintenance/sql/fresh_start_dry_run.sql`
- `maintenance/sql/fresh_start_apply.sql`
- `maintenance/testing/mariadb_phase2_baseline.sql`
- legacy `GetNewsAPI/crypto_news_db.sql`

## Vector Migration Procedure

Prerequisites: MariaDB 11.8, native vector support, guarded database name, no app
schema markers in the vector target, private persistence, read-only inspection,
backup/restore gate, approval, and vector-dependent flags off.

| ID | Purpose |
|---|---|
| `vector-001` | `vector_documents`, `vector_chunks`, and durable `embedding_jobs` schema |
| `vector-002` | Native cosine vector index |
| `vector-003` | Durable `semantic_shadow_assessments` evidence schema |

Execute:

```text
python GetNewsAPI/tasks.py migration_plan vector
python GetNewsAPI/tasks.py migration_check vector
python GetNewsAPI/tasks.py migration_apply vector --backup-confirmed --restore-tested
python GetNewsAPI/tasks.py migration_verify vector
python GetNewsAPI/tasks.py migration_apply vector --backup-confirmed --restore-tested
```

Verification must cover all three migrations, including the native index and
semantic assessment table. The second apply must report
`NO_PENDING_MIGRATIONS`. Use `--allow-disposable` only with the separate test-mode
gate described in the local fallback section.

## Health, Readiness, And API

After starting the web service, check through the intended proxy and, where
available, from inside the service network:

```text
curl -fsS https://<getnewsapi-host>/health
curl -fsS https://<getnewsapi-host>/ready
```

`GET /health` is unauthenticated liveness and performs no external I/O. It should
pass whenever the process is serving HTTP.

`GET /ready` validates effective web configuration and probes the application DB
with `SELECT 1`. It also probes the vector DB only when `VECTOR_ENABLED=true`. It
does not test schema/migration state, providers, WordPress REST, or scheduler jobs.
With all rollout flags off, readiness requires only valid web configuration and
the app DB probe. After vector enablement it requires both DB probes. Pair it with
`migration_verify` and `feature_readiness`; readiness alone is never rollout
approval.

API exposure remains:

- `GET /health`: public liveness; the proxy may rate-limit it.
- `GET /ready`: operational readiness; restrict at the proxy if policy requires.
- `POST /api/publish`: always Bearer-protected by `PUBLISH_API_TOKEN`.
- `GET /api/news`: current unauthenticated read-only behavior; restrict at the
  proxy if it is not intended to be public.
- `/docs`, `/redoc`, `/openapi.json`: development default on, production default
  off through `API_DOCS_ENABLED=false`.

Do not add or assume a new authentication system during this rollout.

## Operational Jobs And Scheduler

Every job runs once, emits structured JSON, and exits. Output work is bounded as
listed below; historical backfill can scan older rows to find that bounded work.
Inspect the offline catalog with:

```text
python GetNewsAPI/tasks.py job_catalog
```

| Command | Class | Key behavior |
|---|---|---|
| `python GetNewsAPI/tasks.py fetch_once` | Recurring baseline | Fetch, score, persist, and schedule one bounded candidate pool |
| `python GetNewsAPI/tasks.py pipeline_once` | Recurring baseline | Process one bounded due batch, then publish one bounded due batch |
| `python GetNewsAPI/tasks.py embedding_ingest [limit]` | Rollout decision | Register recent source/generated documents and jobs; no embedding provider call |
| `python GetNewsAPI/tasks.py embedding_worker [limit]` | Rollout decision | Claim and execute bounded embedding jobs; provider calls occur |
| `python GetNewsAPI/tasks.py process` | Manual | Process one bounded due batch |
| `python GetNewsAPI/tasks.py publish` | Manual | Publish one bounded due batch |
| `python GetNewsAPI/tasks.py embedding_backfill [source|generated] [limit]` | Manual | Register up to the limit of changed versions while scanning deterministic history; no provider call |

`fetch` aliases `fetch_once`; `chained` aliases `pipeline_once`. Prefer the
explicit names in scheduler configuration.

Production has one scheduler owner: DevOps' external scheduler. Keep the web
container's `ENABLE_APSCHEDULER=false`. Configure the current-equivalent baseline:

| Job | Schedule |
|---|---|
| `fetch_once` | Every 30 minutes |
| `pipeline_once` | Every 30 minutes, approximately three minutes after fetch |

Do not define an embedding cadence in the baseline. `embedding_ingest` and
`embedding_worker` scheduling remain rollout decisions; historical backfill is
manual. A busy conflict group returns `SKIPPED_ALREADY_RUNNING` and exits zero.
Repeated skips are an operational signal to inspect duration, overlap, and
scheduler duplication, not permission to add competing schedules.

The fetcher retains its app-DB fetch lock. Process, publish, and pipeline share
the app-DB `pipeline-workflow` conflict group; publish also retains its WordPress
publisher lock. Ingest and backfill share the vector-DB
`embedding-registration` group. Workers use durable row claims and may be scaled
only after controlled validation.

## DEV Staged Validation

Change one stage at a time. Record effective non-secret configuration, exact
commands, JSON result, relevant logs, data evidence, and acceptance decision.
Revert or stop at the first unexplained result.

### Stage 0: Everything Off

Keep every rollout flag false and APScheduler off.

```text
python GetNewsAPI/tasks.py config_check web
python GetNewsAPI/tasks.py config_check pipeline
python GetNewsAPI/tasks.py migration_plan app
python GetNewsAPI/tasks.py migration_check app
python GetNewsAPI/tasks.py migration_verify app
python GetNewsAPI/tasks.py migration_plan vector
python GetNewsAPI/tasks.py migration_check vector
python GetNewsAPI/tasks.py migration_verify vector
python GetNewsAPI/tasks.py feature_readiness
python GetNewsAPI/tasks.py job_catalog
python GetNewsAPI/tasks.py --help
```

Validate `/health`, `/ready`, approved DB/VPN/TLS routes, proxy behavior, log
delivery, restart behavior, and that exactly one scheduler owner is configured.
Startup must perform no migration, provider call, fetch, process, publish,
embedding, or backfill. Do not use a pipeline job merely as a connectivity probe.

### Stage 1: Base Pipeline

After migrations are verified and an explicit live DEV/provider/WordPress test is
approved, invoke from the selected source checkout or immutable image:

```text
python GetNewsAPI/tasks.py fetch_once
python GetNewsAPI/tasks.py pipeline_once
```

Inspect bounded counts, exit status, raw article persistence, scoring and schedule
state, processing/generation, image handling, WordPress result, job locks, and
safe error classes. Do not require a fixed article count; no work is a valid
bounded outcome. Verify failures are visible and retries do not create unexplained
duplicate output.

### Stage 2: Durable Processing

Confirm `app-002` and `app-004` are verified, run `feature_readiness`, then set
only:

```text
PROCESS_DURABLE_CLAIMS_ENABLED=true
```

Restart/redeploy with the same image and run controlled `process` or
`pipeline_once` invocations. Observe claim owner/time, attempt transitions,
`PROCESS_CLAIM_TIMEOUT_MINUTES`, retryable failure release, stale-claim recovery,
replay behavior, throughput, and absence of duplicate concurrent processing.
Disabling this flag returns to the legacy selection path; it does not stop the
processor. Stop scheduler/manual invocations if processing itself must stop.

### Stage 3: Durable Publishing

After Stage 2 acceptance, keep durable processing on and set:

```text
PUBLISH_DURABLE_STATE_ENABLED=true
```

Run a controlled `publish` or `pipeline_once`. Observe deterministic publication
identity, claim/state transitions, saved WordPress post/media identity,
reconciliation after uncertain outcomes, retries/replay, duplicate prevention,
and publisher-lock behavior. Disabling this flag returns to legacy publishing; it
does not halt publishing. Stop job invocations when publication must stop, and
investigate WordPress side effects before replay.

### Stage 4: Deterministic Duplicate Shadow

Confirm `app-007` is verified, then set:

```text
DUPLICATE_SHADOW_ENABLED=true
```

Run controlled processing and collect evidence for `exact_duplicate`,
`same_event_duplicate`, `material_update`, `related_event`, and
`broad_topic_overlap`. The assessment is shadow-only and fail-open. Confirm every
article continues through its normal processing decision and no article is
suppressed solely because of duplicate-shadow evidence.

### Historical Vector Backfill

This is the corpus procedure to approve before activation. Execute the complete
registration-and-worker sequence only after Stages 5 and 6: registration requires
`VECTOR_ENABLED=true`, while worker execution requires both vector and embedding
flags true.

The historical source knowledge path is:

```text
eligible cryptonewsapi source rows
    -> embedding_backfill registration
    -> vector_documents
    -> embedding_jobs
    -> embedding_worker
    -> vector_chunks with VECTOR(1536)
```

`source` backfill scans stored source articles with nonempty text. Recent ingest
uses eligible selected source rows and generated rich rows. `generated` backfill
requires source linkage. Both registration commands are idempotent and make no
provider call; the worker performs embedding calls.

Use small controlled registration waves. The limit bounds changed document/job
registrations, not total historical rows scanned; a run may traverse prior
history to find eligible changed versions:

```text
python GetNewsAPI/tasks.py embedding_backfill source 10
python GetNewsAPI/tasks.py embedding_worker 5
python GetNewsAPI/tasks.py migration_verify vector

python GetNewsAPI/tasks.py embedding_backfill source 100
python GetNewsAPI/tasks.py embedding_worker 10
python GetNewsAPI/tasks.py migration_verify vector
```

The ingest limit bounds documents considered for recent registration. For
backfill, the default 25 and maximum 1000 bound changed registrations; the
`EMBEDDING_BACKFILL_PAGE_SIZE=100` default controls each keyset read, not a total
scan/page ceiling. Begin with small runs and measure DEV rows scanned, duration,
database load, and cost behavior before approving larger registration limits.
The worker default is 5 and maximum is 100 jobs per run, with 100 chunks per job
by default. Choose each next wave only after reviewing registered/existing job
counts, completed/retryable/failed/lost claims, provider calls and spend,
dimensions, index behavior, latency, and storage growth.

`source_article` is independent candidate evidence. `coincourier_generated` is
derivative content and must never be treated as independent factual
corroboration. Generated vectors may later support internal linking, historical
context, RAG, or event continuity, but not independent confirmation.

### Stage 5: Vector Enablement

After `vector-001..003` verify and connectivity is approved, set only:

```text
VECTOR_ENABLED=true
```

Restart/redeploy, check `/ready`, then run:

```text
python GetNewsAPI/tasks.py feature_readiness
```

Vector enablement makes vector-dependent paths and the vector readiness probe
available. It does not create schema, register documents, generate embeddings,
backfill history, schedule a worker, or enable semantic shadow automatically.
Review the vector readiness entry; the overall command can remain nonzero until
later feature prerequisites are present.

### Stage 6: Embedding

Confirm the approved `openai` provider, `text-embedding-3-small` model, 1536
dimensions, `chunk-v1`, API key, vector schema, spend limit, and retry monitoring.
Then set:

```text
EMBEDDING_ENABLED=true
```

Start small:

```text
python GetNewsAPI/tasks.py config_check embedding
python GetNewsAPI/tasks.py embedding_ingest 10
python GetNewsAPI/tasks.py embedding_worker 5
```

Verify `vector_documents`, durable jobs, completed chunks, exact model/version,
1536 dimensions, native index usability, idempotent rerun/no duplicate work,
retryable versus terminal behavior, and stale/lost claim handling. Increase only
in bounded approved steps. Keep historical backfill manual.

### Stage 7: Semantic Retrieval

There is no supported semantic retrieval or calibration command in `tasks.py`.
Do not add an ad hoc production CLI. Validate implementation mechanics locally:

```text
python -m unittest GetNewsAPI.tests.test_semantic_retrieval \
  GetNewsAPI.tests.test_semantic_shadow \
  GetNewsAPI.tests.test_semantic_calibration
```

For real DEV evidence, use approved read-only DB tooling and application logs to
inspect `vector_documents`, `vector_chunks`, `embedding_jobs`, and
`semantic_shadow_assessments`. Confirm a new source query returns ranked historical
`source_article` candidates using native cosine distance and chunk evidence;
respects lookback/publication time; excludes the query document, same source
article, and generated documents from independent candidate evidence; and uses the
current embedding version. Repository tests validate mechanics, but real DEV data
is required to validate usefulness.

### Stage 8: Semantic Shadow

Only after vector schema, embedded source data, and retrieval are accepted, set:

```text
SEMANTIC_SHADOW_ENABLED=true
```

Run controlled processing. Confirm retrieval and semantic assessment evidence are
persisted and the normal pipeline continues. Failures must remain fail-open. No
semantic threshold suppresses, selects, schedules, processes, or publishes an
article, and no production semantic decision policy exists.

Collect deterministic Phase 5 evidence, semantic shadow evidence, and
human-reviewed directed article pairs from real operation. Phase 6C2C remains
blocked until sufficient real labels and evidence exist. Synthetic fixture or
calibration results are research aids, not production cutoff evidence.

### Future: RAG And Event Continuity

Historical source vectors may later support prior-event context, timeline
continuity, related-story retrieval, internal linking, and RAG. None of those is
current rollout behavior, and this runbook does not authorize implementing or
enabling it.

## DEV Acceptance Checklist

- [ ] Application DB connectivity and TLS verified
- [ ] Application schema and migration ledger verified
- [ ] Vector DB connectivity, MariaDB 11.8, and TLS verified
- [ ] Full vector schema through `vector-003` verified
- [ ] `/health` and stage-appropriate `/ready` verified
- [ ] `fetch_once` and `pipeline_once` bounded behavior verified
- [ ] Durable processing claims, retries, replay, and recovery verified
- [ ] Durable publishing identity, reconciliation, and retries verified
- [ ] Deterministic duplicate shadow evidence verified as non-blocking
- [ ] Historical source-vector sample registered and embedded
- [ ] Embedding worker dimensions, retries, idempotency, and index use verified
- [ ] Semantic retrieval ranking, time/source/provenance exclusions verified
- [ ] Semantic shadow evidence and fail-open behavior verified
- [ ] Job conflict locks and `SKIPPED_ALREADY_RUNNING` behavior verified
- [ ] Exactly one scheduler owner; no duplicate recurrence
- [ ] Stdout/stderr logs and alerts verified without secret leakage
- [ ] Web/job restart behavior verified
- [ ] Application state and vector data persist across replacement/restart
- [ ] Provider and WordPress failures are visible and bounded
- [ ] Backup identifiers and tested restore evidence recorded for both DB targets
- [ ] Disk/capacity, restart, provider, DB, job, and shadow monitoring active
- [ ] Emergency flag and scheduler disable paths rehearsed

## Release Candidate Freeze

After DEV acceptance, stop changing the release candidate. Record:

```text
git rev-parse HEAD
docker image inspect <registry-image>:<tag> --format '{{json .RepoDigests}}'
```

Sign off the exact commit, immutable image reference, digest, migration manifest
checksums, and DEV evidence. Production uses that same digest. Do not rebuild with
production-specific source changes; only configuration, endpoints, secrets,
networks, and storage differ.

## Production Preflight And Migration

DEV success does not authorize production mutation. Start production with every
rollout flag off and independently run:

```text
python GetNewsAPI/tasks.py config_check web
python GetNewsAPI/tasks.py config_check pipeline
python GetNewsAPI/tasks.py migration_plan app
python GetNewsAPI/tasks.py migration_check app
python GetNewsAPI/tasks.py migration_verify app
python GetNewsAPI/tasks.py migration_plan vector
python GetNewsAPI/tasks.py migration_check vector
python GetNewsAPI/tasks.py migration_verify vector
python GetNewsAPI/tasks.py feature_readiness
```

Production may be `pending`, `untracked_applied`, `ambiguous`, or `drift` even when
DEV is clean. Inspect it independently and never copy ledger assumptions. Record a
fresh production backup and confirmed restore capability for each target. Resolve
all blocked states through a separately reviewed plan.

Only after approval, apply and verify one target at a time:

```text
python GetNewsAPI/tasks.py migration_apply app --backup-confirmed --restore-tested
python GetNewsAPI/tasks.py migration_verify app
python GetNewsAPI/tasks.py migration_apply app --backup-confirmed --restore-tested

python GetNewsAPI/tasks.py migration_apply vector --backup-confirmed --restore-tested
python GetNewsAPI/tasks.py migration_verify vector
python GetNewsAPI/tasks.py migration_apply vector --backup-confirmed --restore-tested
```

Both repeated applies must report `NO_PENDING_MIGRATIONS`. Stop on any nonzero
result or unexpected output.

## Production Staged Activation

Use a separate approval and observation window for each stage:

1. Deploy the exact DEV-tested image digest with all rollout flags off.
2. Verify proxy, `/health`, stage-appropriate `/ready`, logs, restart, and one
   external scheduler owner.
3. Complete independent production app preflight; reconcile or apply approved app
   migrations and verify.
4. Provision and independently preflight the private MariaDB 11.8 vector target;
   reconcile or apply approved vector migrations and verify.
5. Enable durable processing, observe claims/retries/throughput, and accept.
6. Enable durable publishing, observe identity/reconciliation/replay, and accept.
7. Enable deterministic duplicate shadow, collect non-blocking evidence, and
   accept.
8. Enable vector connectivity, verify `/ready` and feature readiness, and accept.
9. Enable embeddings; run small controlled ingest/worker batches and accept.
10. Run controlled source historical-backfill waves with verification between
    waves.
11. Enable semantic shadow only after retrieval acceptance; observe fail-open
    evidence and normal pipeline continuation.

Never enable every flag together. Never enable semantic suppression or infer a
decision threshold from shadow/calibration output.

## Rollback And Disable Matrix

Schema migrations are forward/additive. "Disable" below means stop invocations or
change a runtime flag and redeploy/restart; it never means automatic schema-down.

| Problem | Immediate safe action | Flag/schedule action | Can app continue? | Data/code action |
|---|---|---|---|---|
| Web deployment failure | Stop rollout; preserve logs; restore previous known image digest | Keep all new flags off | Previous web may continue if healthy | No schema-down; investigate before retry |
| DB migration failure | Stop all later migration/activation work | Keep schema-dependent flags off | Only paths compatible with verified schema | Inspect partial DDL; restore verified backup or use reviewed repair |
| `APPLY_SUCCEEDED_VERIFY_FAILED` | Treat as failed mutation; stop | Keep target-dependent flags off | Unrelated verified paths only | Preserve output; inspect partial effects; restore/repair |
| Durable processing problem | Stop process/pipeline schedules while assessing claims | Set `PROCESS_DURABLE_CLAIMS_ENABLED=false` only to return to legacy behavior | Web and other jobs can continue; processing is not halted by flag alone | State remains; use only reviewed claim recovery; code rollback usually unnecessary |
| Durable publishing problem | Stop publish/pipeline; inspect WP before replay | Set `PUBLISH_DURABLE_STATE_ENABLED=false` only to return to legacy behavior | Web/non-publish jobs can continue | App/WP state remains; reconcile side effects; code rollback usually unnecessary |
| Duplicate shadow problem | Disable evidence collection | `DUPLICATE_SHADOW_ENABLED=false` | Yes, normal pipeline continues | Existing evidence remains; investigate |
| Vector problem | Stop vector-dependent jobs and semantic rollout | `VECTOR_ENABLED=false`; also stop ingest/backfill/worker schedules | Base web/pipeline can continue; `/ready` stops vector probe | Vector data remains; investigate DB/network/index |
| Embedding problem | Stop worker and registration/backfill invocations | `EMBEDDING_ENABLED=false` disables worker execution | Base app/vector storage can continue | Existing vectors/jobs remain; ingest/backfill can still register if invoked, so stop those too |
| Semantic shadow problem | Disable semantic evidence path | `SEMANTIC_SHADOW_ENABLED=false` | Yes, normal pipeline continues | Existing evidence/vectors remain |
| Scheduler overlap | Disable duplicate schedule owner; inspect duration | Keep `ENABLE_APSCHEDULER=false`; pause affected external trigger | Web and unrelated jobs continue | `SKIPPED_ALREADY_RUNNING` preserves data; investigate repeated skips |
| Provider outage | Pause affected fetch/process/publish/worker trigger; retain backlog | No universal provider flag | Web/DB paths and unrelated jobs can continue | Retry after provider recovery; no code rollback by default |

`app-005` releases/reconciles Phase 2 claimed state only. It is not a general
schema rollback. After any migration failure: stop, do not continue, inspect the
possibility of committed partial DDL, and either restore from the verified backup
or execute an explicitly reviewed repair. Do not improvise a down migration.

## Emergency Switch Consequences

| Switch set false | Actual consequence |
|---|---|
| `ENABLE_APSCHEDULER` | Stops only in-process development scheduling; external schedules are unaffected |
| `PROCESS_DURABLE_CLAIMS_ENABLED` | Uses legacy processing selection; does not halt processing or erase claim state |
| `PUBLISH_DURABLE_STATE_ENABLED` | Uses legacy publication path; does not halt publishing or undo WordPress side effects |
| `DUPLICATE_SHADOW_ENABLED` | Stops new deterministic shadow assessments; normal pipeline continues |
| `VECTOR_ENABLED` | Disables vector-dependent runtime paths and vector readiness probe; stored vector data remains |
| `EMBEDDING_ENABLED` | Worker is disabled and new vector generation stops; stored vectors/jobs remain; explicit ingest/backfill registration still requires only vector enablement |
| `SEMANTIC_SHADOW_ENABLED` | Stops new semantic assessment evidence; normal pipeline continues |

To halt base recurring work, pause the external `fetch_once` and/or
`pipeline_once` schedules. Feature flags are not substitutes for scheduler stop
controls.

## Monitoring And Networking

Monitor and alert on:

- web container health, `/health`, `/ready`, restart count, and crash loops;
- application and vector DB connection/TLS failures, lock waits, migration failure,
  verification failure, capacity, and backup status;
- fetch, pipeline, process, publish, embedding worker, WordPress, and provider
  failures plus nonzero command exits;
- semantic-shadow failures and confirmation that fail-open processing continues;
- frequency/duration of `SKIPPED_ALREADY_RUNNING` and duplicate scheduler triggers;
- embedding queue/retry/failure/lost-claim counts, provider use/spend, vector row
  growth, index behavior, and persistent-vector disk usage;
- `/data` capacity and persistence of image reuse/licensing state.

Stdout/stderr is canonical. Dokploy logs, Loki, or another DevOps-selected
aggregation product may collect it; this runbook does not mandate a vendor. File
logging is compatibility-only and defaults off.

Network contract:

```text
Internet -> reverse proxy/TLS -> getnewsapi-web:5000
getnewsapi-web and one-shot jobs -> private application MariaDB
getnewsapi-web and vector jobs -> private vector MariaDB 11.8
publish jobs -> WordPress REST and WordPress DB
jobs -> approved providers and image/source HTTPS hosts
```

The application and vector DBs must not be publicly exposed by GetNewsAPI
Compose. Vector MariaDB remains private. Developer access to remote DEV must be an
intentional VPN/tunnel/firewall grant, and the immutable local container must also
receive the required route and CA mount. Restrict outbound access to approved
destinations where practical.

## Do Not Do This

- Do not run `migration_apply` before `migration_plan` and `migration_check`.
- Do not auto-baseline or adopt untracked schema.
- Do not run fresh-start scripts on an existing environment.
- Do not use `app-005` as schema rollback.
- Do not manually create GetNewsAPI-managed app/vector schema.
- Do not enable every feature simultaneously.
- Do not enable production APScheduler alongside an external scheduler.
- Do not expose MariaDB publicly.
- Do not run historical embedding backfill without a small controlled
  registration limit and DEV scan/load measurement.
- Do not treat CoinCourier-generated content as independent corroboration.
- Do not choose a semantic duplicate cutoff from synthetic calibration data.
- Do not use production as the first end-to-end validation environment.
- Do not assume `/ready` proves provider or schema readiness.
- Do not rebuild different application code for production.

## Command Quick Reference

Run direct source commands from the repository root:

```text
python GetNewsAPI/tasks.py config_check [web|fetch|process|pipeline|publish|embedding]

python GetNewsAPI/tasks.py migration_plan [app|vector|all]
python GetNewsAPI/tasks.py migration_check [app|vector|all]
python GetNewsAPI/tasks.py migration_verify [app|vector|all]
python GetNewsAPI/tasks.py migration_apply [app|vector|all] \
  --backup-confirmed --restore-tested
python GetNewsAPI/tasks.py migration_apply [app|vector|all] --allow-disposable
python GetNewsAPI/tasks.py feature_readiness

python GetNewsAPI/tasks.py job_catalog
python GetNewsAPI/tasks.py fetch_once
python GetNewsAPI/tasks.py pipeline_once
python GetNewsAPI/tasks.py process
python GetNewsAPI/tasks.py publish
python GetNewsAPI/tasks.py embedding_ingest [limit]
python GetNewsAPI/tasks.py embedding_worker [limit]
python GetNewsAPI/tasks.py embedding_backfill [source|generated] [limit]
```

The disposable apply form also requires `APP_ENV=test`,
`MIGRATION_TEST_MODE=true`, and a connected `*_test` database. The production or
persistent-environment form requires `APP_ENV=production` and both attestations.
Do not combine the safety models.

Container and Compose wrappers:

```text
docker build --tag getnewsapi:<git-sha> .
docker run --rm --env-file GetNewsAPI/.env \
  getnewsapi:<git-sha> python tasks.py <supported-command>

docker compose --env-file /secure/path/getnewsapi-provisioning.env \
  -f docker-compose.prod.yml config --quiet

docker compose --env-file /secure/path/getnewsapi-provisioning.env \
  -f docker-compose.prod.yml run --rm getnewsapi-web \
  python tasks.py <supported-command>

docker compose -f maintenance/vector/docker-compose.vector.yml up -d
docker compose -f maintenance/vector/docker-compose.vector.yml ps
docker compose -f maintenance/vector/docker-compose.vector.yml down
```

Replace `<supported-command>` with the command arguments from one of the source
forms above, omitting the host-side `GetNewsAPI/` path inside the image. The
production provisioning file must set `GETNEWSAPI_RUNTIME_ENV_FILE` to the
separate external runtime file and supply required `VECTOR_MARIADB_*` values.
Only the runtime file is injected into web and one-shot job containers.

## DEV To Production Promotion Checklist

### DEV

- [ ] Exact Git commit recorded
- [ ] Immutable image digest recorded
- [ ] Application schema verified
- [ ] Vector schema verified through `vector-003`
- [ ] Base pipeline verified
- [ ] Durable processing and publishing verified
- [ ] Deterministic duplicate shadow verified
- [ ] Historical source vectors sampled
- [ ] Embeddings and native index verified
- [ ] Semantic retrieval verified on real data
- [ ] Semantic shadow and fail-open behavior verified
- [ ] External scheduler, one-shot jobs, and locks verified
- [ ] Monitoring and persistence verified
- [ ] Rollback, scheduler stop, and feature-disable paths verified

### Production

- [ ] Same tested artifact digest selected
- [ ] Production configuration validated offline
- [ ] Production app/vector state independently inspected
- [ ] Fresh backup confirmed for each mutation target
- [ ] Restore capability confirmed and owner identified
- [ ] Required migrations approved, applied, and verified
- [ ] All rollout flags initially off
- [ ] One-stage-at-a-time activation approved
- [ ] Monitoring and alerts active before activation
- [ ] Rollback/incident owner identified

## Phase 7E Boundary

Phase 7D documents the procedure only. It does not start a disposable deployment,
apply a real DEV migration, invoke remote DEV fetch/pipeline, run historical
backfill, enable semantic shadow, or perform production-like acceptance.

The next phase is remote DEV validation when all required endpoints, credentials,
network paths, backups, and approvals are available. Otherwise it is a local Phase
7E clean-room rehearsal using disposable services and no live dependencies.
