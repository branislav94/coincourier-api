# GetNewsAPI Incremental Refactor Plan

## 1. Scope and outcome

This plan follows the current system description in
`GETNEWSAPI_SYSTEM_ARCHITECTURE_AND_VECTOR_ROADMAP.md`. It keeps the existing
synchronous, cron-driven pipeline and proposes only boundaries that solve a
demonstrated operational problem.

Phase 0 is the Flask-to-FastAPI parity migration completed alongside this
document. FastAPI is only the HTTP shell. It does not own provider clients,
database access, processing, publishing internals, or task dispatch.

The non-goals for Phase 0 are publishing extraction, Payload CMS support,
duplicate detection, embeddings, schema changes, provider changes, and a broad
package rewrite.

## 2. Phase 0 runtime contract

Run commands from `GetNewsAPI/`.

Direct local API:

```text
python app.py
```

Local hot reload:

```text
python -m uvicorn app:app --reload --host 127.0.0.1 --port 5000
```

Pipeline commands remain independent of FastAPI:

```text
python tasks.py fetch
python tasks.py process
python tasks.py publish
python tasks.py chained
```

Local API with in-process scheduling:

```text
ENABLE_APSCHEDULER=true
python -m uvicorn app:app --reload --host 127.0.0.1 --port 5000
```

Use `ENABLE_APSCHEDULER=true` only with one Uvicorn server process. Do not use
multiple workers with in-process APScheduler. A reload replaces the application
process and therefore cleanly stops the old process schedulers before the new
process starts its own. Production external cron remains authoritative with
`ENABLE_APSCHEDULER=false`:

```text
ENABLE_APSCHEDULER=false
python app.py
```

The HTTP contract is `GET /health`, `GET /api/news`, and `POST /api/publish`.
The pipeline remains synchronous, and no route uses `BackgroundTasks`.

## 3. Why a restrained structure

A previously considered hierarchy with separate `app/api/core/database/`
`ingestion/processing/analytics/orchestration/shared` packages is excessive for
the current application. Most of those packages would initially contain one
module, force imports through several forwarding layers, and obscure the real
runtime entry points: `tasks.py`, `scheduler.py`, `fetcher.py`,
`gpt_processor.py`, and `publish_to_wp.py`.

The project does not currently have multiple APIs, interchangeable databases,
a general analytics platform, or enough shared code to justify generic core and
shared packages. Boundaries should be added when they isolate target-specific
publishing, stateful claims, duplicate policy, or vector operations. Keeping
stable entry-point files at the top level also preserves cron, deployment, and
operator muscle memory.

## 4. Recommended target tree

This is a direction, not a request to create empty packages.

```text
GetNewsAPI/
    app.py
    tasks.py
    scheduler.py
    config.py
    db.py

    fetcher.py
    gpt_processor.py

    publishing/
        __init__.py
        models.py
        base.py
        service.py
        registry.py
        wordpress/
            __init__.py
            client.py
            publisher.py
            media.py
            taxonomy.py
            seo.py
        payloadcms/
            __init__.py
            client.py
            publisher.py

    image_search/
        ... existing V2 implementation ...

    repositories/
        raw_news.py
        rich_articles.py
        publication.py
        vectors.py

    duplicate_detection/
        identities.py
        event_matching.py
        lexical.py
        policy.py

    vector_store/
        embeddings.py
        repository.py
        jobs.py
        retrieval.py

    migrations/
    tests/
    docs/
```

Only create a package when its corresponding phase begins. In particular,
`repositories/vectors.py` and `vector_store/` do not belong in the tree before
the vector phase.

## 5. Dependency direction

```text
app.py --------------------> application/service entry points
tasks.py ------------------> fetcher / processor / publishing entry points
scheduler.py --------------> the same entry points

fetcher.py ----------------> RawNewsRepository (when extracted)
gpt_processor.py ----------> RichArticleRepository (when extracted)
PublishingService ---------> PublicationRepository
PublishingService ---------> configured Publisher
WordPressPublisher --------> WordPress client modules
PayloadCmsPublisher -------> Payload CMS client modules

duplicate_detection -------> raw/rich repositories
duplicate_detection -------> vector retrieval only as an optional final signal
vector_store --------------> VectorRepository / embedding provider

Forbidden directions:
vector_store -X-> publishing
publishing -X-> embedding generation
domain pipeline -X-> FastAPI
tasks.py -X-> FastAPI
```

Exact identity checks and provider event-ID matching must work without vectors.
Semantic retrieval must fail open: an embedding or vector-store outage records
the failure but does not stop processing or publishing. Raw source records and
generated CoinCourier articles retain distinct models, repositories, IDs, and
vector document types so provenance cannot be blurred.

## 6. Smallest useful repositories

- `RawNewsRepository`: operations that acquire, select, claim, or update raw
  provider news. Keep cohesive SQL close together; do not wrap every query.
- `RichArticleRepository`: generated article reads, writes, processing claims,
  and transitions before publication.
- `PublicationRepository`: target-specific publication claims, idempotency,
  external IDs, attempts, outcomes, and reconciliation state.
- `VectorRepository`: added later for documents, chunks, embedding jobs, and
  similarity search. It is not a generic database layer.

Repositories should return domain-shaped records and own transaction boundaries
that matter for claims. They should not contain provider HTTP logic, prompt
construction, target-specific publication mapping, or FastAPI types.

## 7. Publisher contract

The smallest useful future contract is:

```python
class Publisher(Protocol):
    def publish(
        self,
        article: PublicationArticle,
        image: PublicationImage | None,
        context: PublicationContext,
    ) -> PublicationResult:
        ...
```

`PublicationArticle` carries target-neutral generated content and provenance:
article ID, raw-source ID, title, slug candidate, HTML body, excerpt, categories,
tags, SEO fields, canonical source facts, and intended publication time.

`PublicationImage` carries bytes or a stable local reference, MIME type, alt
text, caption, source-page URL, creator, license name/version/URL, and the image
selection identity. Attribution must remain structured, not reconstructed from
one display string.

`PublicationContext` carries target name, idempotency key, claim/attempt ID,
desired status, correlation ID, and explicit target mapping configuration.

`PublicationResult` carries success/failure status, target, external document
ID, external media ID, public/admin URL when available, whether the operation
created or reconciled a document, and a structured target-specific error.

`PublishingService` should eventually own publication claims, idempotency-key
creation, configured target selection, reconciliation, application database
state, and final structured logs. It must not know WordPress or Payload field
names.

`WordPressPublisher` should own WordPress authentication, taxonomy lookup and
creation, media upload and metadata, post create/update, Yoast behavior, and
WordPress-specific error translation.

`PayloadCmsPublisher` should own Payload authentication, collection mapping,
media creation, document create/update, and Payload status/draft/version
behavior. A small explicit registry keyed by configured target is enough; there
is no need for generic plugin discovery, Ghost, or a custom API publisher.

## 8. Payload CMS feasibility

Payload CMS is feasible behind the same target-neutral contract, but only after
the real Payload collections and access policy are known. Future configuration
can be:

```text
PUBLISH_TARGET=wordpress|payloadcms
PAYLOADCMS_API_URL=
PAYLOADCMS_API_KEY=
PAYLOADCMS_ARTICLES_COLLECTION=posts
PAYLOADCMS_MEDIA_COLLECTION=media
PAYLOADCMS_PUBLISH_STATUS=published
```

`PUBLISH_TARGET` must default to `wordpress`. Payload must remain unavailable
unless explicitly configured and its contract tests pass.

The future mapping decision must cover title, slug, HTML versus Lexical rich
text, categories, tags, SEO fields, media relationship, publication status,
drafts and versions, and localization. The adapter must not assume those field
names or shapes. A discovery spike should capture sanitized collection schemas
and representative mocked request/response fixtures before implementation.

## 9. Incremental phases

### Phase 0: FastAPI parity migration

- Files created: `tests/test_api.py` and this document.
- Files modified: `app.py`, `scheduler.py`, `fetcher.py`, and `requirements.txt`.
- Compatibility wrappers: existing DB, publisher, task, fetcher, processor, and
  `python app.py` entry points remain callable.
- Tests: route shape/status/query, 500 shape, lifespan, import safety, Uvicorn
  arguments, scheduler idempotency, and task dispatch.
- Rollout switch: existing `ENABLE_APSCHEDULER`; production keeps it `false`.
- Rollback: restore the four runtime/dependency files and remove the new API test.
- Risk: framework serialization or lifecycle differences.
- Behavior change: HTTP framework changes and `/health` is added; existing route
  and pipeline behavior is intended to remain unchanged.
- Commit boundary: one isolated FastAPI parity commit after tests pass.

### Phase 1: Publisher models and WordPress adapter (implemented)

- Actual package tree:

  ```text
  publishing/
      __init__.py
      models.py
      base.py
      wordpress/
          __init__.py
          client.py
          publisher.py
          media.py
          taxonomy.py
          seo.py
  ```

- `publish_to_wp.py` is a thin compatibility alias that preserves
  `publish_news_to_wp()`, `slugify()`, and existing image-helper patch surfaces.
- The generic boundary contains only `PublicationArticle`, `PublicationImage`,
  `PublicationContext`, `PublicationResult`, and the synchronous `Publisher`
  protocol. WordPress remains the sole configured target; no registry or service
  was added.
- WordPress authentication/retries, media transport/metadata, taxonomy, post
  payload creation, Yoast writes, and the current batch publisher are separated
  without changing their observable ordering or failure behavior.
- Advisory locking, due-row SQL, image selection coordination, usage recording,
  and the final application `published=1` update remain in the extracted
  WordPress publisher until Phase 2 introduces transaction-owning services.
- Characterization tests cover wrapper/caller compatibility, WordPress payloads,
  authentication, retries, media attribution, taxonomy, Yoast SQL, locking, and
  application published-state behavior with mocks only.
- Rollout switch: none; this is the existing path after a behavior-preserving
  extraction.
- Rollback: restore the pre-extraction `publish_to_wp.py` implementation and
  remove the Phase 1 package/tests.
- Risk: accidental payload or exception translation drift.
- Behavior change: none; the same WordPress requests and database transitions.

### Phase 2: PublishingService and durable publication state

**IMPLEMENTED BUT DISABLED**

- `RawNewsRepository` and `PublicationRepository` use short `SELECT ... FOR
  UPDATE` transactions to assign cryptographically random owner tokens. The
  transaction commits before LLM, image, or WordPress work; owner-token updates
  complete or release the row, and timed-out claims can be recovered.
- `PublishingService` owns claim, durable `coincourier:<rich-id>:<raw-id>`
  identity, local state, media recovery, adapter invocation, and completion.
  WordPress-specific lookup and metadata SQL remain in the WordPress adapter.
- The manual MariaDB 10.4 migration is versioned under
  `maintenance/migrations/`. URL/raw-identity and uniqueness preflights are
  reviewed before indexes are created; no migration runs at application startup.
- WordPress reconciliation checks local `wp_post_id`, then
  `_coincourier_publication_key`. New post identity is written to WordPress and
  attempted in the application DB before image-usage recording and Yoast writes.
  Media IDs are similarly validated or recovered through attachment metadata.
- `publish_to_wp.publish_news_to_wp()` retains `wp_publisher_lock`; `app.py`,
  `tasks.py`, and `scheduler.py` retain their existing public calls.
- Rollout switches are `PROCESS_DURABLE_CLAIMS_ENABLED=false` and
  `PUBLISH_DURABLE_STATE_ENABLED=false` by source default. Apply and verify all
  migration steps before enabling either switch.
- Rollback disables both switches and releases active claims with
  `005_phase2_rollback_state.sql`; additive columns, IDs, and reconciliation
  evidence remain in place.
- Verification: 41 focused mocked/static Phase 2 tests and 128 discovered tests
  pass. No disposable MariaDB integration environment exists, so DDL execution
  remains an operator pre-deployment check.
- Remaining risk: REST post creation and the first durable write span two
  databases. Writing WP identity first and local ID second recovers either
  single-write failure, but a hard process kill in the instruction-sized gap
  immediately after HTTP 201 cannot be made atomic without a WordPress-side
  idempotent create endpoint or registered create-time metadata.

### Phase 3: Payload CMS adapter

- Files created: `publishing/payloadcms/client.py`, `publisher.py`, fixtures, and
  contract tests.
- Files modified: registry, config, and `.env.example` for documented Payload keys.
- Compatibility wrappers: WordPress wrapper remains; service chooses one explicit
  target.
- Tests: auth, article/media mapping, create/update, drafts, failures, idempotency,
  and sanitized sandbox fixtures.
- Rollout switch: `PUBLISH_TARGET=wordpress|payloadcms`, default `wordpress`.
- Rollback: set `PUBLISH_TARGET=wordpress`; reconcile any Payload shadow records.
- Risk: incorrect collection assumptions or rich-text/media relationship mapping.
- Behavior change: only explicitly configured deployments publish to Payload.
- Commit boundary: schema decision record, client, adapter, then opt-in wiring.

### Phase 4: Additional repository extraction if justified

- Phase 2 already introduced the transaction-owning `RawNewsRepository` and
  `PublicationRepository`. Add `rich_articles.py` only if generated-article
  persistence gains a cohesive transaction boundary beyond its current caller.
- Files modified: the narrow call sites whose transactions move into repositories.
- Compatibility wrappers: old helper functions delegate until all current callers
  are proven.
- Tests: SQL contract fixtures, transaction/rollback, claim races, and state parity.
- Rollout switch: per-repository call-site flags only if shadow reads are practical.
- Rollback: restore wrapper delegation to legacy SQL; no schema rollback required.
- Risk: transaction scope changes and connector cursor-shape differences.
- Behavior change: none.
- Commit boundary: one repository and its callers per commit.

### Phase 5: Exact, event-ID, and lexical duplicate detection

**Implemented but disabled; observational only.**

- `duplicate_detection/` now owns conservative URL identity, Unicode-aware title
  normalization, SHA-256 source-content fingerprints, token-set Jaccard, and
  lightweight structured entity/date/number/action extraction. It makes no AI or
  embedding call.
- Every active CryptoNews pull requests `id,eventid,rankscore`. Returned `eventid`
  remains nullable and is persisted as `event_id`; an omitted value no longer
  overwrites a previously stored non-null event ID.
- The versioned policy emits `exact_duplicate`, `same_event_duplicate`,
  `material_update`, `related_event`, or `broad_topic_overlap` with individual
  evidence and reason codes. Exact identity is provider article ID, conservative
  canonical URL, or sufficiently long normalized source-content hash. Event ID is
  strong event evidence, never an exact identity or a suppression rule.
- `DuplicateAssessmentRepository` compares at most 200 selected or processed raw
  rows published within the configured 72-hour window. It excludes the current
  row, orders candidates deterministically, and upserts one audit row per
  `(article_id, candidate_article_id, policy_version)`.
- Manual migrations `006_phase5_duplicate_preflight.sql` and
  `007_phase5_duplicate_shadow.sql` add only `duplicate_assessments`. Existing
  `news_id`, `event_id`, `canonical_url`, and `title_hash` are not duplicated or
  rewritten. Shadow comparison computes its conservative identities from source
  values; only pairwise evidence is persisted, avoiding new raw-table columns.
- The hook runs in `gpt_processor.process_one()` before search enrichment and
  rewrite. It is guarded by `DUPLICATE_SHADOW_ENABLED=false`; disabled means no
  duplicate query or write. Any analysis error is logged and processing continues.
- No classification changes `chosen_for_publish`, `scheduled_for`, `processed`,
  `published`, processing claims, publication claims, or WordPress behavior.
- Rollback: set `DUPLICATE_SHADOW_ENABLED=false`; retain additive assessments for
  evaluation. There is intentionally no enforcement flag in this phase.
- Remaining risk: deterministic rules need production shadow evaluation before
  any eligibility policy is designed. Phase 6 remains vector schema and
  asynchronous embedding jobs; no vector dependency exists in Phase 5.

### Phase 6A: MariaDB vector storage foundation

- Status: implemented locally and source-default disabled; not deployed.
- Files created: independent migrations under `maintenance/vector_migrations/`, a
  reproducible local `mariadb:11.8` service under `maintenance/vector/`, the
  `vector_store` models/connection/repository boundary, and unit/integration tests.
- Files modified: configuration and documentation only. `tasks.py`, processing,
  publishing, and deterministic duplicate analysis have no vector-store import.
- Schema: `vector_documents`, `vector_chunks` with `VECTOR(1536)`, and storage-only
  `embedding_jobs`, all in the separate `coincourier_vectors` database.
- Tests: deterministic fake-vector round trips, dimensions, idempotency, provenance
  filters, native cosine ordering/index plans, transactions, and migration reruns on
  disposable MariaDB 11.8.9.
- Rollout switch: `VECTOR_ENABLED=false`. Disabled startup opens no vector connection.
- Behavior change: none in fetch, process, publish, image, or duplicate decisions.
- Rollback: keep the optional service absent or disabled; the application DB is
  independent and has no vector migration.

### Phase 6B: Embedding production and asynchronous jobs

- Planned only: finalize provider/model/dimension policy, production chunking,
  embedding client calls, job claiming/retries, backfill, and a cron-safe task.
- Compatibility boundary: article processing and publishing must not wait for vectors.
- Tests required: chunk determinism, hosted-client mocks, retries, stale claims,
  resumable backfill, model-version isolation, and outage fail-open behavior.
- Risk: embedding cost/backlog, source-retention policy, and dimension/model migration.

### Phase 7: Semantic duplicate shadow mode and retrieval

- Files created: `vector_store/retrieval.py`, duplicate assessment persistence,
  evaluation fixtures, and retrieval audit tests.
- Files modified: duplicate policy for optional semantic evidence and processor
  context assembly for measured historical retrieval.
- Compatibility wrappers: vector failure returns no semantic signal and preserves
  the previous path.
- Tests: known duplicate/non-duplicate corpus, thresholds, outages, stale vectors,
  provenance filters, and historical citation/audit records.
- Rollout switch: `SEMANTIC_DEDUPE_MODE=off|shadow|enforce_high_confidence` and a
  separate `HISTORICAL_RETRIEVAL_ENABLED=false`.
- Rollback: set both switches off; retain assessments for analysis.
- Risk: semantic false positives, context leakage, latency, and model drift.
- Behavior change: none in shadow; later only evaluated high-confidence enforcement.
- Commit boundary: retrieval, shadow assessment, evaluation, then optional rollout.

### Phase 8: Optional gpt_processor extraction

- Files created: only proven cohesive modules such as validation, prompt building,
  and processing service modules.
- Files modified: `gpt_processor.py` delegates while keeping its public function.
- Compatibility wrappers: `process_news_with_gpt()` remains stable for tasks/scheduler.
- Tests: golden prompts, hard/soft validation, provider routing, persistence, and
  complete old-versus-new contract runs with providers mocked.
- Rollout switch: `PROCESSOR_SERVICE_V2=false` if parallel implementations coexist.
- Rollback: switch wrapper back to the legacy body.
- Risk: hidden coupling in a large, operationally mature module.
- Behavior change: none.
- Commit boundary: one cohesive extraction per commit, only after boundaries settle.

### Phase 9: Optional fetcher extraction

- Files created: only modules justified by measured size/coupling, likely provider
  client, scoring, selection, and scheduling policy.
- Files modified: `fetcher.py` remains the compatibility facade.
- Compatibility wrappers: `run_fetch_cycle()`, `fetch_all_news()`, and scheduler
  functions retain signatures.
- Tests: provider fixtures, scoring, quota/scheduling, locks, transactions, and full
  mocked cycle parity.
- Rollout switch: component-level flags only when shadow comparison is possible.
- Rollback: wrapper returns to legacy implementations.
- Risk: changing mature timing, locks, ranking, or quota semantics during movement.
- Behavior change: none.
- Commit boundary: provider client, pure scoring, selection policy, then orchestration.

## 10. Deferred abstractions and justification thresholds

| Deferred item | Condition that would justify it |
| --- | --- |
| Top-level `app/` package rewrite | Multiple independently deployed applications share substantial code. |
| FastAPI route-package hierarchy | The API gains several cohesive route groups with separate ownership. |
| Generic dependency framework | Constructor wiring becomes repeated, error-prone, and difficult to test manually. |
| Ghost publisher | A funded Ghost deployment has a verified schema and launch plan. |
| Custom API publisher | A concrete target exists with stable authentication and field contracts. |
| Generic analytics framework | Several production metrics pipelines share transformations and retention rules. |
| Pricing engine | Pricing becomes a real product requirement with owned data and policy. |
| Generic `shared/` utilities | Multiple bounded packages need the same stable, domain-neutral implementation. |
| One script per task command | Commands need independent packaging, permissions, or deployment lifecycles. |
| Plugin discovery | Third parties must add publishers without changing this repository. |
| Event bus | Multiple consumers need durable fan-out and replay beyond current cron jobs. |
| Message broker | Database-backed jobs cannot meet measured throughput or reliability needs. |
| Microservices | Independent scaling/ownership outweighs transactional and operational cost. |
| Local LLM hosting | Privacy, cost, or latency evidence supports owning model operations. |

## 11. Rollout and rollback discipline

Every phase should begin with fixture-backed contract tests and an operational
baseline. New decision logic starts in shadow mode. A rollout switch changes one
dimension at a time, logs both paths with stable correlation IDs, and has an
explicit removal criterion after confidence is established.

Phase 0 rollback is especially small: restore Flask in `app.py`, restore the
scheduler start-only functions, replace FastAPI/Uvicorn/httpx requirements with
Flask, and remove `tests/test_api.py`. Docker and external cron commands do not
change because `python app.py` and `tasks.py` remain stable.

Future schema phases use forward-compatible additive migrations. Rollback turns
off readers/writers before considering destructive cleanup. Publication and
duplicate audit data should normally be retained for reconciliation and review.
