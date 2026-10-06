# GetNewsAPI Dokploy DEV Handoff

Operator preparation updated on 2026-10-06 for parallel DEV Compose cutover.
The user reports that the existing root-Dockerfile Application resource has
passed live application, durable process/publish, provider, embedding/vector,
retrieval, and semantic-shadow acceptance. Keep that working resource and its
databases intact. This repository update performs no remote deployment or data
copy. Follow the [generic deployment runbook](GETNEWSAPI_DEPLOYMENT_RUNBOOK.md)
for independently approved mutations, migrations, recovery, and monitoring.

## Git and Dokploy Compose contract

| Setting | Required contract |
|---|---|
| Repository | https://github.com/branislav94/coincourier-api.git |
| DEV branch | `dev` -> test/DEV environment |
| Historical initial implementation SHA | `54872b16d1defd899ccce2614c81d7a51cb7640c`; record the reviewed current DEV commit separately |
| Production branch | `main` -> production later; never select it for DEV |
| Production SHA to preserve | `349a4233880632d1c125c7f9dfec66b0a6bf1c0c` |
| Target Dokploy resource | Second temporary DEV Compose resource, standard Docker Compose mode; existing Application stays intact until cutover succeeds |
| Compose path / service | `./docker-compose.dev.yml` / `getnewsapi-dev` |
| Build context / Dockerfile | Repository root / `./Dockerfile` |
| Runtime command / working directory | `python app.py` / `/app` |
| Internal port / bind | `5000` / `0.0.0.0`; one Uvicorn worker, reload disabled |
| Container user | UID/GID `10001:10001` |
| Liveness | `GET /health`; existing image healthcheck |
| Persistent application storage | Named `getnewsapi-dev-state` at `/data`, physical name scoped to the Dokploy Compose project |
| Runtime invariants | `APP_ENV=production`, `API_DOCS_ENABLED=false`, `ENABLE_APSCHEDULER=false`, `PYTHONUNBUFFERED=1` |
| Network / exposure | External `dokploy-network`; internal port 5000 only; no published host port |

Verify the saved Compose resource selects the reviewed current `dev` commit and
`./docker-compose.dev.yml`; record the actual SHA/image digest and project
identity. The SHA above is dated baseline evidence, not the new deployment target.
The reviewed build includes OpenAI Luna enrichment and
`EMBEDDING_FRESH_START_AFTER_UTC`. Production/LIVE uses `./docker-compose.yml`
with `getnewsapi-prod` only under separate approval.
`APP_ENV=production` is the persistent-runtime safety mode; the services and
credentials remain DEV. Keep runtime secrets out of build arguments and Git.

The target topology is one API Compose service plus the existing external DEV
application MariaDB, WordPress/its DB, and managed MariaDB 11.8 vector resource.
Compose owns only the API process lifecycle; it creates no database, WordPress,
worker, cron, or scheduler service. Preserve the populated vector DB and all
verified migration state. `maintenance/vector/docker-compose.vector.yml` is
LOCAL DEVELOPMENT / VECTOR VALIDATION ONLY, never persistent Dokploy DEV,
production, or shared infrastructure.

Dokploy's Compose environment editor writes an uncommitted `.env` next to the
Compose file. The service loads `${GETNEWSAPI_RUNTIME_ENV_FILE:-.env}` through
`env_file`, ensuring runtime variables actually enter the container. CLI
`--env-file` alone supplies interpolation, not complete container injection.
The optional file-path override is for sanitized local validation or an approved
external runtime file. Never include provisioning/root variables in that file.
See the [Dokploy Compose environment/storage contract](https://docs.dokploy.com/docs/core/docker-compose)
and [Compose networking/domain guidance](https://docs.dokploy.com/docs/core/docker-compose/domains).

## Parallel DEV cutover: stages A through I

Execute these operator steps only after reviewing this contract. Do not delete
the working Application first. Preserve its accepted runtime values, including
rollout flags and both freshness cutoffs; the new-environment bootstrap example
later in this document is not a replacement for that working configuration.

1. **Stage A — parallel resource.** Create a second temporary DEV Compose resource
   from branch `dev`, path `./docker-compose.dev.yml`, service `getnewsapi-dev`.
   Use standard Docker Compose mode, the existing DEV runtime environment, and
   the same external application DB, vector DB, WordPress, and WordPress DB.
   Verify private `dokploy-network` routes. Attach no public/production API domain
   yet; enable no new schedule or manual mutating job.
2. **Stage B — read-only acceptance.** Require `/health=200` and `/ready=200`
   through the intended private service path. In the new service terminal:

   ```sh
   cd /app
   python tasks.py config_check web
   python tasks.py config_check fetch
   python tasks.py config_check process
   python tasks.py config_check publish
   python tasks.py config_check pipeline
   python tasks.py config_check embedding
   python tasks.py feature_readiness
   ```

   `config_check` is offline; `feature_readiness` reads existing DB state and
   performs no migration or provider/WordPress call. Require
   `durable_processing=ready`, `durable_publishing=ready`,
   `duplicate_shadow=ready`, `vector=ready`, `embedding=ready`, and
   `semantic_shadow=ready`. Stop on unexpected configuration, connectivity,
   migration drift, or readiness failures; do not replace the DB or apply schema
   as a cutover workaround.
3. **Stage C — persistent state.** Inspect the old Application `/data` mount and
   record its actual identity, backup, capacity, and ownership. Choose either a
   reviewed platform mount that safely reuses the confirmed volume, or the new
   project-scoped `getnewsapi-dev-state` volume. Never guess a Docker volume name.
   If a new volume is used, arrange one consistent copy of existing `/data`
   during the writer pause in Stage E. Verify UID/GID 10001:10001 ownership and
   write access, `/data/cache/stock_images`, and
   `/data/stock_image_usage.json`. Test persistence across a controlled service
   recreate without deleting its volume. A copy inspected while the old jobs
   are still writing is not the final cutover copy.
4. **Stage D — disabled jobs.** Create the four Dokploy COMPOSE jobs in the UTC
   schedule below, targeting `getnewsapi-dev` in `/app`, all disabled.
5. **Stage E — quiesce old writers.** Disable every old Application schedule and
   prevent manual processing/publishing during the handoff. Wait for active runs
   to finish. If Stage C selected a new volume, perform the approved consistent
   `/data` copy now and verify ownership, integrity, and persistence before
   proceeding. If reusing storage, verify neither resource can write concurrently.
   No database export, deletion, reset, migration, or vector copy is required.
6. **Stage F — one scheduler set.** Enable the new Compose jobs only after the
   old jobs are disabled/drained and state is verified. Never leave both sets
   enabled; keep `ENABLE_APSCHEDULER=false` throughout.
7. **Stage G — domain cutover.** If DEV has an API domain, move/attach that DEV
   route to `getnewsapi-dev`, internal port 5000. Do not attach production domains
   or publish a host port. Verify the saved Dokploy service/domain selection.
8. **Stage H — observe.** Verify `/health`, `/ready`, logs, state persistence, and
   one full scheduled sequence with bounded structured results and one scheduler
   owner. Preserve redacted evidence and investigate unexpected overlap/errors.
9. **Stage I — retain rollback resource.** Stop the old Application only after
   healthy replacement operation is confirmed. Do not delete it immediately.
   Observe; delete obsolete resources only later after review, preserving all
   retained volumes and separately managed databases.

For rollback, first disable/drain new jobs before restoring any old schedule or
domain route. Reconcile which `/data` copy contains the newest accepted usage
state; do not silently revert to stale storage. Existing provider/WordPress side
effects remain real and must not be replayed merely to test cutover.

## Database and WordPress resource checklists

### Application MariaDB

The application DB is separate from the vector DB. Migration tooling requires
MariaDB >= 10.4.0, a selected DB matching `DB_NAME`, and compatible pre-existing
`cryptonewsapi` and `rich_crpytonews` tables (the latter spelling is intentional).
Confirm the current runtime columns and article identities, not just table names.
No fixed application DB name is required by the guard.

**An empty application DB is not supported by the normal migration chain.**
Stop for a separately reviewed bootstrap if the base schema is absent. Do not
use `GetNewsAPI/crypto_news_db.sql`, fresh-start utilities, or destructive test
fixtures as a persistent DEV bootstrap. Historical rows are not needed for
process startup; meaningful historical vectorization needs an approved DEV dataset.

- [ ] MariaDB version confirmed.
- [ ] Database name confirmed; app and vector targets remain separate.
- [ ] Dedicated runtime user exists with reviewed permissions.
- [ ] `cryptonewsapi` exists with compatible current runtime fields.
- [ ] `rich_crpytonews` exists with compatible current runtime fields.
- [ ] Existing/historical DEV data decision confirmed.
- [ ] Backup exists and identifier/owner recorded.
- [ ] Restore owner/process identified; tested-restore evidence available before APPLY.
- [ ] Private route from API exists; DB port is not public.
- [ ] TLS policy chosen and recorded.

### Vector MariaDB

| Requirement | Value/owner |
|---|---|
| Engine | MariaDB **11.8.x** specifically |
| Persistent DB name | **`coincourier_vectors`**; do not use `coincourier_vectors_dev` |
| Private route / public 3306 | Required / prohibited |
| Persistent DB storage | Required; DevOps owns `/var/lib/mysql` persistence |
| User | Dedicated GetNewsAPI application user; non-root |
| Root/provisioning secret in API | Prohibited, including `VECTOR_MARIADB_ROOT_PASSWORD` |
| Manual vector tables/indexes | Prohibited; GetNewsAPI migration CLI owns them |
| Backup/restore | DevOps responsibility, including owner and tested process |

- [ ] Exact version and database name verified.
- [ ] Dedicated user, private route, persistence, and TLS policy recorded.
- [ ] Backup/restore and incident owners identified.
- [ ] No application base tables exist in the vector target.
- [ ] No manual GetNewsAPI schema creation or provisioning secrets in API runtime.

The guard requires matching configured/selected names, MariaDB 11.8 native
cosine capability, and separation from application-table markers. A fresh empty
vector database is supported. The application owns:

| Migration | Purpose |
|---|---|
| `vector-001` | `vector_documents`, `vector_chunks`, `embedding_jobs`, `VECTOR(1536)`, provenance/version constraints |
| `vector-002` | Native `idx_vector_chunks_embedding_cosine` index with `DISTANCE=cosine` |
| `vector-003` | `semantic_shadow_assessments` for bounded semantic evidence |

### Isolated DEV WordPress

Use the DEV domain chosen by DevOps. `test.coincourier.io` is a suggestion, not
a confirmed destination. WordPress infrastructure is externally provisioned;
this repository supplies clients, not a WordPress stack.

- [ ] Site is isolated from production and has working HTTPS.
- [ ] `/wp-json/` is available.
- [ ] Dedicated user/application password supports posts, categories, tags, and media.
- [ ] Direct WordPress DB access references the **same site** as REST credentials.
- [ ] Uploads and database storage are persistent; backup ownership is recorded.
- [ ] Intended Yoast behavior is confirmed before functional SEO acceptance.

`WP_API_URL` is the site base URL without a trailing slash, not `/wp-json` or
`/wp-json/wp/v2`: code appends those endpoint paths. Direct WP DB reads/writes
are needed even with durable publication off. Prefix discovery uses an options
table with `wp_` fallback. `SEO_PLUGIN=yoast` reflects intended behavior, but
changing that variable does not switch adapters; the current writer is Yoast-specific.

## Runtime environment contracts

All values below are runtime-only, injected through the approved secret store.
Identifiers/hosts are non-secret configuration; passwords/tokens are secrets.
No actual connection values or credentials belong in this document or Git.
Defaults are from current code, not permission to leave deployment policy implicit.

| Application DB variables | Classification / source default |
|---|---|
| `DB_HOST`, `DB_NAME`, `DB_USER` | Required, non-secret identifiers; no defaults |
| `DB_PASSWORD` | Required secret; no default |
| `DB_PORT` | Non-secret, defaultable: `3306` |
| `DB_CONNECT_TIMEOUT_SECONDS` | Non-secret, defaultable: `5` |
| `DB_SSL_ENABLED`, `DB_SSL_VERIFY_CERT`, `DB_SSL_VERIFY_IDENTITY`, `DB_SSL_CA` | TLS-dependent; exact policy below |

| Vector DB variables | Classification / source default |
|---|---|
| `VECTOR_DB_HOST`, `VECTOR_DB_USER` | Required for vector inspection/use; non-secret; no defaults |
| `VECTOR_DB_PASSWORD` | Required secret for vector inspection/use; no default |
| `VECTOR_DB_NAME` | Explicitly set `coincourier_vectors`; source default matches |
| `VECTOR_DB_PORT` | Non-secret, defaultable: `3306` |
| `VECTOR_DB_CONNECT_TIMEOUT_SECONDS` | Non-secret, defaultable: `5` |
| `VECTOR_DB_SSL_ENABLED`, `VECTOR_DB_SSL_VERIFY_CERT`, `VECTOR_DB_SSL_VERIFY_IDENTITY`, `VECTOR_DB_SSL_CA` | TLS-dependent; exact policy below |

Do not inject `VECTOR_MARIADB_*` or equivalent database provisioning/root secrets
into GetNewsAPI. `.env.provisioning.example` describes local-only vector provisioning,
not the remote API environment. Migration tooling uses the same `DB_*` and
`VECTOR_DB_*` names; a later authorized migration job may receive separately
reviewed DDL credentials without changing the application's runtime user.

| WordPress variables | Classification / source default |
|---|---|
| `WP_API_URL`, `WP_USERNAME` | Required for publishing; non-secret; no defaults |
| `WP_APP_PASSWORD` | Required publishing secret; no default |
| `WP_HTTP_CONNECT_TIMEOUT_SECONDS` | Defaultable: `10` |
| `WP_HTTP_READ_TIMEOUT_SECONDS` | Defaultable: `60` |
| `WP_DB_HOST`, `WP_DB_NAME`, `WP_DB_USER` | Required for publishing; non-secret; no defaults |
| `WP_DB_PASSWORD` | Required publishing secret; no default |
| `WP_DB_PORT` | Defaultable: `3306` |
| `WP_DB_CONNECT_TIMEOUT_SECONDS` | Defaultable: `5` |
| `WP_DB_SSL_ENABLED`, `WP_DB_SSL_VERIFY_CERT`, `WP_DB_SSL_VERIFY_IDENTITY`, `WP_DB_SSL_CA` | TLS-dependent; exact policy below |
| `SEO_PLUGIN` | Default `yoast`; current adapter remains Yoast-specific |

### TLS policy: record each endpoint separately

| Target | Enable TLS | Verify certificate | Verify identity/hostname | CA path |
|---|---|---|---|---|
| Application DB | `DB_SSL_ENABLED` | `DB_SSL_VERIFY_CERT` | `DB_SSL_VERIFY_IDENTITY` | `DB_SSL_CA` |
| WordPress DB | `WP_DB_SSL_ENABLED` | `WP_DB_SSL_VERIFY_CERT` | `WP_DB_SSL_VERIFY_IDENTITY` | `WP_DB_SSL_CA` |
| Vector DB | `VECTOR_DB_SSL_ENABLED` | `VECTOR_DB_SSL_VERIFY_CERT` | `VECTOR_DB_SSL_VERIFY_IDENTITY` | `VECTOR_DB_SSL_CA` |

Current source defaults for all three are TLS enabled `true`, certificate
verification `false`, identity verification `false`, and no CA path. These are
compatibility defaults, **not a policy recommendation**. Compose leaves each
managed endpoint's reviewed TLS policy to the existing runtime variables.

Certificate verification requires TLS enabled and CA configuration. Identity
verification depends on certificate verification. The CA file must be mounted
at the specified container path and readable by the application user.

- [ ] Application DB's effective TLS/verification/CA policy explicitly recorded.
- [ ] WordPress DB's effective TLS/verification/CA policy explicitly recorded.
- [ ] Vector DB's effective TLS/verification/CA policy explicitly recorded.
- [ ] Required CA files and permissions confirmed; credentials remain secrets.

### Sanitized first-time bootstrap template

This example is for a genuinely new, unaccepted environment. Do not copy its
all-off flags or blank cutoffs over the accepted DEV runtime during cutover.
Replace every placeholder in Dokploy's secret/configuration store before use.
Do not create or commit a real `.env`. The TLS lines reproduce source defaults
for review: change them to each recorded policy before deployment, including CA
paths when required. Do not inject blank numeric settings; omit unused settings
or use valid reviewed values because configuration parses them at import.

```dotenv
APP_ENV=production
API_DOCS_ENABLED=false
READINESS_DB_TIMEOUT_SECONDS=3
PUBLISH_API_TOKEN=<SECRET>

ENABLE_APSCHEDULER=false
PROCESS_DURABLE_CLAIMS_ENABLED=false
PUBLISH_DURABLE_STATE_ENABLED=false
DUPLICATE_SHADOW_ENABLED=false
VECTOR_ENABLED=false
EMBEDDING_ENABLED=false
SEMANTIC_SHADOW_ENABLED=false
IMAGE_SEARCH_ENGINE=v1
USE_SOURCE_IMAGES=false
MIGRATION_TEST_MODE=false
RUN_GROK_TEXT_SMOKE=false
RUN_GROK_IMAGE_SMOKE=false
PIPELINE_FRESH_START_AFTER_UTC=
EMBEDDING_FRESH_START_AFTER_UTC=

FILE_LOGGING_ENABLED=false
LOG_LEVEL=INFO
WRITABLE_STATE_DIR=/data
STOCK_IMAGE_CACHE_DIR=/data/cache/stock_images
STOCK_IMAGE_USAGE_PATH=/data/stock_image_usage.json

DB_HOST=<DEV_APP_DB_HOST>
DB_PORT=3306
DB_NAME=<DEV_APP_DB_NAME>
DB_USER=<DEV_APP_DB_USER>
DB_PASSWORD=<SECRET>
DB_CONNECT_TIMEOUT_SECONDS=5
DB_SSL_ENABLED=true
DB_SSL_VERIFY_CERT=false
DB_SSL_VERIFY_IDENTITY=false
DB_SSL_CA=

VECTOR_DB_HOST=<DEV_VECTOR_HOST>
VECTOR_DB_PORT=3306
VECTOR_DB_NAME=coincourier_vectors
VECTOR_DB_USER=<DEV_VECTOR_USER>
VECTOR_DB_PASSWORD=<SECRET>
VECTOR_DB_CONNECT_TIMEOUT_SECONDS=5
VECTOR_DB_SSL_ENABLED=true
VECTOR_DB_SSL_VERIFY_CERT=false
VECTOR_DB_SSL_VERIFY_IDENTITY=false
VECTOR_DB_SSL_CA=
```

This template is for first deployment/read-only preflight, not accepted DEV
cutover or live pipeline operation. The flag values match source defaults;
production mode and persistent
paths deliberately override development defaults in `.env.example`. The cache
is replaceable; image-usage JSON affects reuse and must persist across replacements.
File logging starts off; stdout/stderr is the initial log destination.

### Optional fresh-start boundaries

`PIPELINE_FRESH_START_AFTER_UTC` controls processing/publishing eligibility;
`EMBEDDING_FRESH_START_AFTER_UTC` independently controls new embedding document/job
registration by both recent `embedding_ingest` and manual `embedding_backfill`.
Unset or blank preserves existing behavior. For an approved DEV boundary, both
can independently use `2026-10-04 18:00:00` UTC; this is an example, not a default
or a configured deployment value. UTC timestamps with `Z` or an explicit offset
are also accepted; offsets are normalized to UTC.

The embedding boundary is inclusive: the underlying `cryptonewsapi.insertDate`
must be at or after it. Generated articles use their existing `raw_article_id`
link to that raw row; missing/unresolved links or a missing raw insertion time
are excluded while the cutoff is enabled. Publication/selection dates do not
replace this timestamp. Existing eligibility rules still apply.

This setting does not delete application history, vectors, or jobs, filter
already queued worker jobs, start backfill, or enable any rollout flag.

### Provider credentials before approved job/profile tests

| Operation | Required names and conditions |
|---|---|
| Fetch | `CRYPTO_NEWS_TOKEN`, `OPENAI_API_KEY` |
| Process | `OPENAI_API_KEY` for Luna web-search enrichment plus selected writer keys. Defaults select Grok plus OpenAI fallback: `GROK_API_KEY` and `OPENAI_API_KEY` |
| Publish/generated images | WordPress REST/DB plus selected image-provider keys. Default routing uses Grok/OpenAI; current publisher import also initializes OpenAI unconditionally, so supply `OPENAI_API_KEY` even for Grok-only publishing |
| Stock images | `PEXELS_API_KEY` / `PIXABAY_API_KEY` when those providers are used; missing keys skip them. `OPENVERSE_CLIENT_ID` and `OPENVERSE_CLIENT_SECRET` are optional paired credentials for V2; anonymous search is supported |
| Embedding worker | `OPENAI_API_KEY`, verified vector schema, `VECTOR_ENABLED=true`, `EMBEDDING_ENABLED=true` |

`GROK_API_KEY` takes precedence; `XAI_API_KEY` is its fallback alias.
`pipeline_once` processes then publishes; it does not fetch. Provider/WP secrets
are not required merely to boot the initial web process or prove `/health` with
scheduler/features off. Add them before the corresponding profile/job is tested.

### OpenAI factual enrichment

Use the existing `OPENAI_API_KEY`; there is no separate enrichment secret.
Before an approved process or pipeline run, review these non-secret settings:

```text
ENRICHMENT_MODEL=gpt-5.6-luna
ENRICHMENT_REASONING_EFFORT=low
ENRICHMENT_SEARCH_CONTEXT_SIZE=low
ENRICHMENT_MAX_OUTPUT_TOKENS=1200
```

The Responses API request uses `reasoning={"effort": "low"}`, only
`tools=[{"type": "web_search", "search_context_size": "low"}]`,
`tool_choice="required"`, and `store=False`. Search must run for enrichment.
Validation requires a non-empty model, `low` reasoning, a search-context value of
`low`, `medium`, or `high`, and an output-token limit of 1 through 4096.
The response remains bounded factual context for the existing Grok-primary,
OpenAI-fallback writer. It does not write the final article or change embedding,
image, persistence, or publishing behavior. Failed enrichment retains the
processing attempt's failure/retry path; provider errors must remain credential
safe. Account/model access is confirmed only by separately approved live
acceptance, never by `config_check`. See the [official model contract](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
and [hosted web-search documentation](https://developers.openai.com/api/docs/guides/tools-web-search).

## Platform acceptance and additional read-only preflight

### Dokploy health/routing checklist

- [ ] Build/deploy record matches the reviewed current DEV SHA; actual image digest recorded.
- [ ] Unique DEV Compose project; `./docker-compose.dev.yml` and `getnewsapi-dev` selected.
- [ ] Internal port `5000`; proxy/domain route configured if public API access is wanted.
- [ ] `GET /health` returns 200.
- [ ] `/data` mounted persistently and writable by UID/GID `10001:10001`.
- [ ] Application DB reachable privately.
- [ ] Vector DB reachable privately.
- [ ] WordPress reachable over HTTPS when its stage is inspected.
- [ ] No database port publicly exposed.
- [ ] Stdout/stderr logs visible; immutable image digest recorded.
- [ ] CPU/memory limits, backup owner, and incident owner recorded.

Retain the reviewed image/platform hardening: non-root user, read-only root
filesystem, bounded writable `/tmp`, init, dropped capabilities, and
`no-new-privileges`. Canonical Compose explicitly uses external `dokploy-network`;
verify managed DB reachability and approved provider egress there. Dokploy's
domain tooling may add platform routing networks/labels; inspect its saved
preview and service selection without removing the required private route.

| Endpoint | Proves | Does not prove |
|---|---|---|
| `GET /health` | Process liveness, without external I/O | DB, schema, WordPress, providers, migration state |
| `GET /ready`, vector off | Web config validation and app DB `SELECT 1` | Schema/version/ledger, WordPress, providers |
| `GET /ready`, vector on | Adds vector DB `SELECT 1` | Migrated vector schema or full DEV acceptance |

`/ready` returns 503 for required configuration/connectivity failures. Even an
empty DB can pass its connectivity probe; table/schema inspection remains required.
Startup with scheduler off runs no migrations, providers, or jobs. All-off
rollout flags do **not** disable manual processing/publishing or authenticated
`POST /api/publish`; do not invoke them as initial connectivity probes.

### Additional migration inspection: Dokploy container terminal

Use the deployed image's runtime environment, with file logging off. Run one
command at a time, retain redacted output/exit status, and honor stop conditions.

```sh
cd /app
python tasks.py config_check web
python tasks.py config_check pipeline
python tasks.py migration_plan app
python tasks.py migration_check app
python tasks.py migration_verify app
python tasks.py migration_plan vector
python tasks.py migration_check vector
python tasks.py migration_verify vector
python tasks.py feature_readiness
```

`config_check` is offline. Migration plan/check/verify and `feature_readiness`
read DB state; they do not apply migrations, create the ledger, or insert ledger
entries. They do not call WordPress/providers. Explicit targets avoid the CLI
default `all`. No read-only command above authorizes a subsequent APPLY.

For a genuinely new environment using the minimal template, `config_check pipeline` can report missing WP/provider
secrets; record these deferred-profile gaps. Clean `pending` migrations can be
normal before first application, later preflights may be deferred, and
`migration_verify` fails until the full chain is applied and verified.
`feature_readiness` inspects **both DB targets even with vector disabled** and
can remain nonzero while migration/embedding prerequisites are absent. Review
individual results rather than interpreting every nonzero result as a web blocker.

### Stop conditions

If either target reports `untracked_applied`, `ambiguous`, or `drift`, **STOP**.
Do not auto-adopt, baseline, repair, or apply another migration. Return the
redacted plan/check/verify evidence to the application team. Unknown ledger IDs,
inconsistent dependencies, wrong target/name/version, missing application base
tables, and unexplained failures also require review.

`pending` alone is not an adoption or repair case. Keep flags off and obtain
separate migration approval. The migration tool has no general automatic rollback.

## New-environment migration/activation work: separate approval

**DO NOT RUN AS PART OF THE ACCEPTED DEV COMPOSE CUTOVER.** Existing app/vector
migrations and populated DEV data are retained. The following procedures are for
new or separately reviewed environments, not an instruction to rebuild the
working DEV schema.

Normal application sequence: `app-001`, `app-002`, `app-003`, `app-004`,
`app-006`, `app-007`; the preflight entries are 001, 003, and 006. No application
migration APPLY runs during initial setup. Explicitly exclude `app-005` recovery,
fresh-start utilities, disposable test baseline, and legacy `crypto_news_db.sql`.
Use the generic runbook's approved application migration procedure later.

Future authorized vector sequence, inside `/app`:

```sh
python tasks.py migration_plan vector
python tasks.py migration_check vector
python tasks.py migration_apply vector --backup-confirmed --restore-tested
python tasks.py migration_verify vector
```

Persistent DEV uses `APP_ENV=production` and both backup/restore attestations.
The switches attest to real backup and tested-restore evidence; the CLI does
not create backups or test restores. Never use `--allow-disposable` for retained
DEV. Keep rollout flags off during migration; stop on partial-apply or
post-apply verification failures and follow the runbook. Do not run SQL manually.

After infrastructure acceptance and separately reviewed migrations, use this
future order, with evidence and approval at each stage:

1. Baseline fetch/pipeline acceptance against DEV WordPress and approved providers.
2. Durable processing, then durable publication.
3. Deterministic duplicate shadow.
4. Vector enablement after `vector-001..003` verification.
5. Approved historical vector registration/backfill; registration may precede paid
   embedding work with `EMBEDDING_ENABLED=false`.
6. After separate approval, set `EMBEDDING_ENABLED=true`; perform bounded embedding
   worker acceptance, then approved corpus expansion.
7. Semantic retrieval validation using the runbook; there is no supported retrieval
   task command or separate retrieval flag.
8. Semantic shadow acceptance; evidence remains observational, not publication policy.
9. A 48-hour DEV soak with agreed monitoring, incident ownership, and review.

Before embedding profiles, explicitly supply `EMBEDDING_MODEL=text-embedding-3-small`,
`EMBEDDING_CHUNKER_VERSION=chunk-v1`, and `EMBEDDING_DIMENSIONS=1536`.
For new environments, do not enable recurrence until the corresponding readiness
and live acceptance gates pass. The current DEV cutover uses the intended
schedule below while preserving its accepted runtime rollout values.
Record the scheduling/timezone policy: current day accounting uses fixed UTC+02,
not DST-aware timezone behavior. Record chosen throughput limits rather than
assuming `.env.example` values equal omitted-variable source defaults.

## Dokploy-owned DEV schedule

Configure these Dokploy COMPOSE jobs against `getnewsapi-dev`, working directory
`/app`, using UTC. Do not implement cron or a scheduler inside the API container,
in a sidecar, or on the host through this repository.

| UTC cron | Command |
|---|---|
| `0,30 * * * *` | `python tasks.py fetch_once` |
| `2,32 * * * *` | `python tasks.py embedding_ingest 25` |
| `3,33 * * * *` | `python tasks.py embedding_worker 5` |
| `5,35 * * * *` | `python tasks.py pipeline_once` |

Create these jobs disabled at Stage D. Disable/drain old Application jobs at
Stage E, then enable the Compose jobs at Stage F and verify one full sequence.
Never leave both scheduler sets enabled. Keep `ENABLE_APSCHEDULER=false` in both
resources. Backfill remains manual. Production schedules require separate
approval and target `getnewsapi-prod`, never the DEV service.

## Evidence DevOps returns to the application team

Return through the approved operational channel, with secret values omitted:

- [ ] GetNewsAPI: deployed commit SHA, immutable image digest, `/health` status,
      `/ready` result, `/data` persistence/ownership, and redacted preflight evidence.
- [ ] Application DB: host, port, DB name, username, exact version, TLS policy,
      base-schema confirmation, historical-data decision, backup/restore status.
- [ ] WordPress: DEV URL, username, application password provisioned (no value),
      DB host/port/name/username, matching-site confirmation, TLS policy, Yoast confirmation.
- [ ] Vector: exact MariaDB version, host/port, DB name, application user,
      persistence, TLS policy, backup/restore status, private/not-public confirmation.
- [ ] Platform: domain/proxy route, logging location, resource limits, backup owner,
      incident owner, and approval/stage records.

Transfer any required secrets only through the approved secret store. Never place
plaintext passwords/tokens, real `.env` contents, or secret DSNs in Git or documentation.

## Safe first diagnostics

These are first inspection steps for authorized operators, not destructive fixes.

| Initial failure | Safe first diagnostic |
|---|---|
| Dockerfile build fails | Read the first build error; verify commit, root context, Dockerfile path, and dependency-install logs |
| Container starts then exits | Inspect startup logs for config parsing/production validation; verify required app DB fields and publish token exist without printing values |
| `/health` fails | Inspect process logs and probe route/internal port 5000 from the approved service path |
| `/ready` returns 503 | Inspect its failed check and redacted config output; do not infer schema readiness from later success |
| `/data` permission denied | Inspect mount target and ownership/permissions against UID/GID 10001 |
| App DB connection refused | Confirm configured DEV host/port, private route, and DB resource health |
| Vector DB connection refused | Confirm managed endpoint/private route and inspect `migration_plan vector`; vector-off `/ready` does not probe it |
| WordPress unreachable | Inspect chosen DEV DNS/HTTPS route and `/wp-json/` availability with the authorized operator |
| TLS/CA verification error | Compare recorded policy with injected flags; inspect CA mount/path, readability, and certificate hostname |
| Wrong DB name | Compare configured/selected database evidence; vector persistent name must be `coincourier_vectors` |
| Empty application DB | Stop for application-team bootstrap review; do not import the legacy dump/test fixture |
| Ambiguous existing migration state | Retain redacted inspection evidence and stop; no automatic adoption, baseline, repair, or further APPLY |

### Historical deployment failure

Before this contract update, the reported earlier build succeeded, then the
deployment command selected the old root production-shaped Compose. That retired
definition fixed a global container name,
and Docker rejected creation because the name already existed, before application
startup. Repository evidence does not explain the UI/command filename mismatch.

The working corrective deployment used a uniquely named Dockerfile Application.
The new canonical Compose files likewise leave container naming to the platform,
with distinct DEV/production service aliases. **Do not delete or rename an unknown
production container to satisfy DEV.** Retain the working Application until the
parallel replacement is verified.

## Source authority

The initial application contract is historical evidence from the SHA above.
The current Compose contract follows the reviewed DEV tree; the optional
embedding cutoff follows the reviewed current configuration
and [registration queries](../GetNewsAPI/repositories/embedding_articles.py).
Relevant sources: [Dockerfile](../Dockerfile),
[remote DEV Compose](../docker-compose.dev.yml),
[remote production Compose](../docker-compose.yml),
[configuration](../GetNewsAPI/config.py), [application](../GetNewsAPI/app.py),
[runtime validation](../GetNewsAPI/runtime/config_validation.py),
[CLI](../GetNewsAPI/tasks.py), [migration inventory](../GetNewsAPI/deployment/migrations.py),
[migration service](../GetNewsAPI/deployment/service.py),
[target guards](../GetNewsAPI/deployment/preflight.py),
[job catalog](../GetNewsAPI/operations/jobs.py),
[WordPress modules](../GetNewsAPI/publishing/wordpress/),
[vector migrations](../maintenance/vector_migrations/), and
[generic deployment runbook](GETNEWSAPI_DEPLOYMENT_RUNBOOK.md).
