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

### Phase 1: Publisher models and WordPress adapter

- Files created: `publishing/models.py`, `base.py`, `registry.py`, and focused
  `publishing/wordpress/` modules plus tests.
- Files modified: `publish_to_wp.py` becomes a thin compatibility wrapper;
  configuration changes only if target-neutral constructor wiring needs it.
- Compatibility wrappers: `publish_news_to_wp()` retains its signature and result.
- Tests: golden WordPress payloads, media attribution, taxonomy, Yoast, and wrapper
  parity using mocks.
- Rollout switch: internal `WORDPRESS_ADAPTER_V2=false` during shadow comparison.
- Rollback: switch off the adapter and use the preserved legacy function body.
- Risk: accidental payload or exception translation drift.
- Behavior change: none; the same WordPress requests and database transitions.
- Commit boundary: models/interface, then adapter modules, then wrapper wiring.

### Phase 2: PublishingService and durable publication state

- Files created: `publishing/service.py`, `repositories/publication.py`, a focused
  publication-state migration, and reconciliation tests/jobs.
- Files modified: publisher wrapper and task/scheduler wiring call the service.
- Compatibility wrappers: `publish_news_to_wp()` delegates to WordPress through
  the service while preserving callers.
- Tests: atomic claims, concurrent claim rejection, idempotent retry, external-ID
  persistence, crash recovery, reconciliation, and structured logs.
- Rollout switch: `PUBLISHING_SERVICE_V2=false`, enabled first in shadow/claim-only
  mode and then for a small publication cohort.
- Rollback: disable service routing; preserve new state for diagnosis.
- Risk: claim deadlocks or incorrect reconciliation after partial target success.
- Behavior change: intentional idempotency and recoverable publication attempts.
- Commit boundary: schema/repository, claims, reconciliation, then wiring.

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

### Phase 4: Repositories needed by claims and publication state

- Files created: `repositories/raw_news.py` and `rich_articles.py` only for cohesive
  claim/state operations not already covered by publication repository.
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

- Files created: `duplicate_detection/identities.py`, `event_matching.py`,
  `lexical.py`, `policy.py`, focused persistence migration if required, and tests.
- Files modified: ingestion/selection call sites and repositories that expose
  identity/event facts.
- Compatibility wrappers: existing canonical URL/title-hash checks remain active
  and feed the new policy.
- Tests: normalized URL/title identity, transitive event groups, provider event ID,
  lexical thresholds, provenance separation, and false-positive corpora.
- Rollout switch: `DUPLICATE_POLICY_MODE=shadow|enforce_exact|enforce_lexical`.
- Rollback: return to `enforce_exact` or existing exact checks.
- Risk: false positives suppressing legitimate new angles.
- Behavior change: exact checks first; lexical blocking only after shadow evidence.
- Commit boundary: identities, event matching, lexical shadow, then policy rollout.

### Phase 6: MariaDB vectors and embedding jobs

- Files created: vector migrations, `repositories/vectors.py`,
  `vector_store/embeddings.py`, `repository.py`, `jobs.py`, and tests.
- Files modified: `tasks.py` only to add an explicit cron-safe embedding job; config
  for model/version/batch settings.
- Compatibility wrappers: article processing and publishing do not wait for vectors.
- Tests: chunk determinism, model versioning, retries, stale jobs, similarity query,
  connector capability, and fail-open behavior.
- Rollout switch: `VECTOR_JOBS_ENABLED=false`; then enable asynchronous cron batches.
- Rollback: disable jobs and reads; keep vector tables for later cleanup.
- Risk: MariaDB capability/performance variance and embedding cost/backlog.
- Behavior change: background vector records only; no selection decision yet.
- Commit boundary: schema/capability, repository, embedding client, then job command.

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
