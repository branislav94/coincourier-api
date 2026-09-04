# GetNewsAPI System Architecture and Vector Roadmap

Audit date: 2026-07-26

Branch audited: `pipeline-throughput-hybrid-images`

Pushed code baseline: `ea3ba334 Add embedding ingestion and worker operations`,
plus the uncommitted Phase 6C1 retrieval/evaluation foundation described here.

Operational evidence: `getnewsapi-20260726_085522.log.txt` (2026-07-25T19:19:02Z through an incomplete run beginning 2026-07-26T00:47:30Z)

Status labels used throughout this document:

- **CURRENT**: present and reachable in the audited code.
- **IMPLEMENTED BUT DISABLED**: present in code but not selected by the default configuration.
- **PLANNED**: recommended work; it does not exist yet.
- **OPTIONAL FUTURE**: useful later, but not required for the duplicate-defense objective.

Repository evidence is authoritative only for the checked-in code and schema artifacts. No database, WordPress, CryptoNews, or model-provider connection was made for this audit. The checked-in SQL dump is older than the runtime code, so any statement about a newer runtime column is an inference from SQL issued by the application, not a claim that the production schema was inspected.

Phase 2 durable claims and WordPress reconciliation are now **IMPLEMENTED BUT
DISABLED**. Their additive migration is manual and both source-default feature
flags remain false until an operator applies and verifies it.

Deterministic Phase 5 duplicate analysis is also **IMPLEMENTED BUT DISABLED**.
`DUPLICATE_SHADOW_ENABLED=false` is the source default. When explicitly enabled
after its manual migration, it records pairwise evidence before LLM work but never
changes selection, processing, or publication eligibility.

Phase 6A vector storage, Phase 6B1 deterministic chunking/job processing, and
Phase 6B2 controlled ingestion/worker/backfill operations are **IMPLEMENTED
LOCALLY BUT DISABLED**. They use a separate MariaDB boundary and explicit lazy
task commands; none is wired into fetch, process, publish, image, scheduler, or
duplicate behavior. Production scheduling, provider rollout, and all semantic
duplicate behavior remain planned. Phase 6C1 bounded source-only semantic
retrieval and synthetic labeled evaluation are **IMPLEMENTED LOCALLY BUT
DISABLED**; they have no automatic caller and make no duplicate decision.

## 1. Purpose and scope

**CURRENT**

GetNewsAPI fetches crypto-news candidates, stores and scores raw stories, selects a daily queue, rewrites due stories with hosted LLMs, stores rich articles, finds or generates a featured image, and creates published WordPress posts. The primary application tables are `cryptonewsapi` and the intentionally misspelled `rich_crpytonews`.

This document answers four operational questions:

1. What does the current system do, including its state transitions and failure behavior?
2. Which exact duplicate and idempotency controls exist at each stage?
3. Why can separate URLs covering the same event both be selected and published?
4. How should exact, event, lexical, semantic, topic, and publication defenses be introduced without making the main pipeline depend on embeddings?

This document began as a read-only architecture audit. The current Phase 5 update
adds only disabled-by-default deterministic shadow code, configuration, tests, and
manual additive migration files. It changes no provider/model/image routing,
selection decision, publication behavior, route, or infrastructure.

## 2. High-level system diagram

**CURRENT**

```mermaid
flowchart LR
    A[CryptoNews API] --> B[fetcher._pull_batch]
    B --> C[(cryptonewsapi)]
    C --> D[API and hosted-AI scoring]
    D --> E[chosen_for_publish and scheduled_for]
    E --> F[gpt_processor.process_news_with_gpt]
    F -. DUPLICATE_SHADOW_ENABLED=true .-> R[(duplicate_assessments)]
    R -. always continue .-> G
    F --> G[Gemini search enrichment]
    F --> H[Grok or OpenAI rewrite and validation]
    G --> H
    H --> I[(rich_crpytonews)]
    I --> J[publish_to_wp.publish_news_to_wp]
    J --> K[Image Search V1 by default]
    J -. IMAGE_SEARCH_ENGINE=v2 .-> L[Image Search V2]
    K --> M[Stock image or hosted generation]
    L --> M
    M --> N[WordPress media]
    N --> O[WordPress post]
    O --> P[Yoast metadata in WP DB]
    P --> Q[rich_crpytonews.published = 1]
```

The persistent source path is still keyed primarily by `news_url`. Pull-local
identity checks and raw URL uniqueness remain unchanged as eligibility controls.
The optional Phase 5 hook compares deterministic exact/event/fact/lexical evidence
before expensive LLM work, but its results are audit-only and cannot close the
selection or publication duplicate gap until a separately reviewed enforcement
phase.

## 3. Repository/module map

**CURRENT**

| Module or artifact | Responsibility | Important references |
|---|---|---|
| `GetNewsAPI/app.py` | Flask API; optional local scheduler startup | routes at lines 12-30 and 32-54; scheduler switch at 56-67 |
| `GetNewsAPI/config.py` | Environment parsing and provider/DB configuration | runtime controls 21-59; LLM routing 61-109; images 112-180; WP/DB 182-205 |
| `GetNewsAPI/db.py` | Application DB connection factory | lines 1-5 |
| `GetNewsAPI/fetcher.py` | CryptoNews pulls, in-memory exact dedupe, persistence, scoring, selection, scheduling | URL/title identities 164-207; pulls 429-552; persistence 734-800; selection 901-1081; cycle/lock 1146-1279 |
| `GetNewsAPI/gpt_processor.py` | Due-row selection, search enrichment, sticky LLM routing, rewrite, validation, rich persistence | due count 144-179; provider routing 481-943; search 1121-1198; rewrite 1203-1368; validation 1443-1643; persistence/process 1675-1981 |
| `GetNewsAPI/publish_to_wp.py` | Image routing, media upload, WP taxonomy/post creation, Yoast metadata, published state | publisher lock 184-223; due query 226-260 and 683-722; image switch 1250-1385; upload 1388-1548; publication 1642-1790 |
| `GetNewsAPI/scheduler.py` | Local APScheduler orchestration | independent process/publish error handling 27-69; 30-minute schedule 72-111 |
| `GetNewsAPI/tasks.py` | CLI entry points suitable for external cron | commands and chained behavior 16-83 |
| `GetNewsAPI/provider_smoke.py` | Prints routing configuration; optional live Grok calls behind explicit flags | flow 50-78; live gates 81-125; report 128-165 |
| `GetNewsAPI/stock_images.py` | V1 Pexels/Pixabay query, scoring, cache, reuse, and sequential selection | scoring 454-539; adapters 542-648; sequential selection 651-760 |
| `GetNewsAPI/image_search/*` | Provider-neutral V2 models, adapters, cache/retry, scoring, licensing, download validation, reuse, and selection | models 8-93; registry 15-41; selection 36-285 |
| `GetNewsAPI/tests/test_image_search.py` | Mocked image adapter, policy, ranking, reuse, routing, and attribution tests | transitive dedupe at 511; WordPress attribution flow at 972; V1 default routing at 1036 |
| `GetNewsAPI/crypto_news_db.sql` | Historical schema/data dump, not a current migration | generated for MariaDB 10.4 at lines 1-8; table definitions 30-44 and 70-84; URL unique keys 110-120 |
| `maintenance/sql/fresh_start_*.sql` | Manual queue inspection and cutoff cleanup | dry-run checks 5-149; apply transaction/default rollback 6-66 |
| `Dockerfile` | Python 3.11 image and application startup | lines 2-12 |
| `docker-compose.yml` | One Flask application service with bind mount | lines 1-17 |
| `.env.example` | Sanitized runtime/throughput/provider/DB setting inventory | lines 1-116 |

There is no migration framework, current schema snapshot, cron declaration, CI configuration, or prior architecture README in the audited repository. SQL conventions consist of one old dump and manually reviewed maintenance scripts, with the destructive fresh-start script defaulting to `ROLLBACK`.

## 4. Runtime and deployment modes

**CURRENT - Flask-only container**

`docker-compose.yml:1-17` builds one Python 3.11 service, maps host port 5001 to container port 5000, bind-mounts `GetNewsAPI` to `/app`, and runs `python app.py`. `.env.example:2` sets `ENABLE_APSCHEDULER=false`, so the example deployment serves Flask without background jobs unless explicitly overridden. There is no database service, health check, worker service, or cron service in the compose file.

**CURRENT - local APScheduler**

When `ENABLE_APSCHEDULER=true`, `app.py:56-67` starts the scheduler once with Flask reloading disabled. The fetch scheduler runs immediately and every 30 minutes with one in-process instance, coalescing, a 60-second misfire grace period, and jitter (`fetcher.py:1284-1315`). A separate scheduler runs processing and then publishing every 30 minutes, first after three minutes, with `max_instances=1`, coalescing, one-hour misfire grace, and jitter (`scheduler.py:72-111`). These controls prevent overlap only inside that Python scheduler instance.

**CURRENT - external cron entry points**

`tasks.py:16-83` exposes `fetch`, `process`, `publish`, and `chained`. `chained` attempts publication even when processing raises (`tasks.py:43-56`). No server cron expression is checked in, so its cadence, environment, timeout, and overlap policy cannot be audited from this repository. Fetch and publish have cross-process MariaDB advisory locks; processing does not.

**CURRENT - HTTP routes**

- `POST /api/publish` calls the publisher and returns success when the call returns (`app.py:12-30`).
- `GET /api/news` returns the seven newest rich rows without authentication or field filtering (`app.py:32-54`).

## 5. Environment configuration groups

**CURRENT**

| Group | Variables and defaults visible in code/example | Notes |
|---|---|---|
| Runtime | `ENABLE_APSCHEDULER=false`, `FLASK_DEBUG=false`, `PIPELINE_FRESH_START_AFTER_UTC` | parsed at `config.py:21-59` |
| Throughput | `DAILY_TARGET`, active window/share, max age, fetch pool/score limit, process min/max/lookahead, publish max | example at `.env.example:6-17`; module defaults differ from some example values |
| LLM | primary `grok`, fallback `openai`, provider keys/base URL/models/reasoning/output limits | `config.py:64-109` |
| Search enrichment | `GOOGLE_API_KEY`; search itself is a code constant `USE_WEB_SEARCH=1` | `gpt_processor.py:73-93` |
| Image mode | API/source flags, `hybrid`, `stock_first`, generated-image primary/fallback | `config.py:112-127` |
| Image search | default engine `v1`; V2 providers, dimensions, limits, license allowlist, retry/exhaustion behavior | `config.py:129-180` |
| Duplicate shadow | disabled by default; 72-hour lookback and `v1` policy version | `config.py` |
| WordPress | REST URL/user/application password and separate WP DB settings | `config.py:182-205` |
| Application DB | user/password/host/port/name; TLS verification is disabled in the connector dictionary | `config.py:187-196` |

Secrets are environment sourced and must remain absent from logs and documentation. The supplied log contains behavior and identifiers, not credentials.

## 6. Fetching and raw ingestion

**CURRENT - provider requests**

`fetcher._pull_batch()` performs three pulls (`fetcher.py:458-552`):

| Pull | Parameters relevant to identity |
|---|---|
| Rank-sorted multi-ticker | `extra-fields=id,eventid,rankscore` (`fetcher.py:487-495`) |
| General category | `extra-fields=id,eventid,rankscore` (`fetcher.py:489-494`) |
| Multi-ticker | `extra-fields=id,eventid,rankscore` (`fetcher.py:505-511`) |
| Optional video | same extra fields, but `ALLOW_VIDEO=False` (`fetcher.py:513-523`) |

CryptoNews event ID is explicitly requested on all three active feeds and the
optional video feed. The code reads `eventid` and writes it to nullable runtime
`cryptonewsapi.event_id`; when a later provider item omits it, the upsert preserves
an existing non-null value. The supplied log cannot prove provider population.
Event ID remains absent from scoring and selection and is used only as shadow
classification evidence before processing when the disabled-by-default hook is
enabled.

**CURRENT - pull-local exact dedupe**

Within one aggregated pull, the first candidate wins when any of these exact values was already seen (`fetcher.py:525-549`):

- non-empty `news_id`;
- `_clean_url(news_url)`;
- SHA-256 of the lowercased, outer-whitespace-trimmed title.

The pull-local `_clean_url()` and title hash retain their legacy behavior so this
shadow phase cannot change ingestion eligibility. The separate Phase 5 identity
module uses conservative parser-based canonicalization: scheme and host are
lowercased, fragments/default ports are removed, a terminal slash is normalized,
and only `utm_*` plus a fixed obvious-tracker allowlist is removed. Meaningful query
parameters and publisher-specific paths are preserved. Its title comparison uses
NFKC, case folding, punctuation/separator normalization, and whitespace collapse
while retaining words, names, dates, and numbers; the stored source title is not
mutated.

**CURRENT - persistence**

The retained pool is pre-ranked and truncated to `POOL_SIZE` before persistence (`fetcher.py:550-552`). `_insert_or_update()` inserts raw content and the news/event/rank/title identities, then uses `ON DUPLICATE KEY UPDATE` (`fetcher.py:734-800`). The repository schema declares only `news_url` unique (`crypto_news_db.sql:110-120`); it declares no unique provider news ID, canonical URL, title hash, event ID, or content hash. The live schema has additional columns because the runtime SQL uses them, but their indexes cannot be verified without a current schema artifact.

There is no persistent raw body/content-hash column. Phase 5 computes SHA-256 from
NFKC/case-folded, HTML-text-extracted, whitespace-normalized source text only when
at least 80 characters exist. The hash never contains source text, remains separate
from event similarity, and only pairwise equality is persisted. `event_id` is a
classification signal, never an exact-rejection key.

## 7. Scoring, selection and scheduling

**CURRENT - scoring**

The pre-blend uses API rank, recency, source prior, and ticker prior (`fetcher.py:209-237`). Up to the configured pool/score limit is sent to a hard-coded OpenAI `gpt-5-mini` scoring call, independent of primary rewrite-provider routing (`fetcher.py:637-732`). Recent unchosen rows receive a final weighted score from hosted-AI importance, rank, recency, source, and ticker values (`fetcher.py:828-880`).

**CURRENT - selection**

Breaking candidates above the threshold and within the four-hour comparison window are scheduled immediately (`fetcher.py:953-969`). Non-breaking candidates fill the remaining `DAILY_TARGET`, split across active/off-peak UTC windows, ordered by final importance and publication time (`fetcher.py:971-1048`). `_assign_schedule()` sets `chosen_for_publish=1`, `selected_at`, and `scheduled_for` (`fetcher.py:1052-1081`).

Selection does not compare candidates against:

- other candidates in the same event;
- already selected rows, except indirectly through `chosen_for_publish=0` on the candidate itself;
- processed or published stories with different URLs;
- source concentration;
- normalized title similarity;
- entity, number, or date overlap;
- rolling topic or angle quotas.

The daily counter counts selected rows, not successfully published posts (`fetcher.py:901-926`). Breaking stories are selected before remaining capacity is computed, so later breaking arrivals can raise the selected count above `DAILY_TARGET` (`fetcher.py:953-975`). The supplied log demonstrates a rise from 30 to 33.

## 8. LLM processing and sticky provider routing

**CURRENT**

Automatic batch size is the number due within the lookahead, clamped to `PROCESS_BATCH_MIN..PROCESS_BATCH_MAX`. The source-default legacy path retains its existing eligible-row query and has no processing advisory lock or transactional claim. When `PROCESS_DURABLE_CLAIMS_ENABLED=true` after migration, `RawNewsRepository` selects the same eligible order under `FOR UPDATE`, assigns an owner token, and commits before LLM work. Claims expire after `PROCESS_CLAIM_TIMEOUT_MINUTES`.

For each row:

1. When explicitly enabled, deterministic duplicate shadow analysis reads recent
   selected/processed candidates, records classifications, and always continues.
   Any repository or policy error is caught and logged before continuing.
2. Gemini search grounding requests up to three recent facts when the code constant is enabled (`gpt_processor.py:1121-1198`).
3. The rewrite tries the configured primary LLM and then the configured fallback (`gpt_processor.py:876-921`). Defaults are Grok then OpenAI (`config.py:65-70`, `config.py:105-109`).
4. The provider that produced the valid rewrite becomes sticky for repair (`gpt_processor.py:1353-1368`, `gpt_processor.py:1532-1625`). A short body may first receive same-provider expansion (`gpt_processor.py:747-811`).
5. On legacy-path failure, the row remains `processed=0`. On the durable path it also becomes `processing_status='retryable'`, clears its claim, and stores a bounded error class; success still sets `processed=1`.

Provider HTTP calls use bounded retry/backoff. Grok retries up to seven times with jitter (`gpt_processor.py:591-669`); OpenAI handles network/transient statuses and malformed/truncated output (`gpt_processor.py:947-1115`); Gemini retries only 429/503 (`gpt_processor.py:1159-1172`).

## 9. Hard and soft article validation

**CURRENT**

Hard validation requires title, body, category, hashtags, sentiment, SEO focus/slug/meta, image alt, at least one paragraph, and at least 450 plain-text words (`gpt_processor.py:1795-1846`). The prompt asks for 550-750 words, at least two H2 headings, restricted source links, and specific SEO placement (`gpt_processor.py:1251-1321`).

Readability and Yoast-style checks are calculated separately (`gpt_processor.py:1443-1513`). Soft failures include sentence length, transitions, keyword placement/density, metadata/title length, H2 count, external links, and target length (`gpt_processor.py:363-397`). A hard-valid article is stored even when soft checks remain after repair, with a warning (`gpt_processor.py:1624-1643`).

The prompt examples repeatedly use `bitcoin dominance` as the SEO-focus example (`gpt_processor.py:1236-1248`, `gpt_processor.py:1307-1320`). The operational log also shows source article 14744, titled about one million congressional contacts and the CLARITY Act, receiving `seo_focus='bitcoin dominance'`. This is evidence that phrase repetition can be introduced or amplified by rewriting and SEO generation; it does not establish source-event duplication.

## 10. Rich article persistence

**CURRENT**

`store_rich_news()` normalizes one to three categories and inserts title/body, processing-time `publish_date`, source, categories, tags, sentiment, tickers, source image URL, and SEO fields (`gpt_processor.py:1675-1748`). It upserts by whatever unique key triggers, with the repository dump showing only unique `news_url`. On duplicate, only SEO focus/slug/meta are updated (`gpt_processor.py:1715-1745`).

Important persistence properties:

- The legacy path still joins raw/rich by unique `news_url`. After migration, the durable path also writes `raw_article_id`; preflight proves that the URL backfill maps each rich row to one raw row before a unique index is added. No foreign key is inferred.
- There is no check by raw ID, provider news ID, title, slug, event, or content similarity.
- Rich insertion commits before raw `processed=1` is committed through a separate connection (`gpt_processor.py:1745-1748`, `gpt_processor.py:1776-1793`, `gpt_processor.py:1883-1887`).
- A crash between those commits leaves a rich row with the raw row retryable. URL uniqueness prevents a second rich row for that same URL, but the expensive rewrite can repeat.
- Generated `schema_jsonld` and `image_alt` are not included in the rich INSERT (`gpt_processor.py:1717-1745`), even though later code tries to read `schema_jsonld` and media alt text. In the current path, media alt falls back to SEO focus/title (`publish_to_wp.py:1538-1544`).

No transaction guarantees "one raw row produces one rich row and becomes processed" as a single atomic state change.

## 11. Image Search V1

**CURRENT - default**

`IMAGE_SEARCH_ENGINE` defaults to `v1` in both code and `.env.example` (`config.py:129-131`; `.env.example:64-66`). In V1, article/SEO terms produce up to three queries, Pexels is searched first, then Pixabay only if needed (`stock_images.py:651-760`; `publish_to_wp.py:1250-1297`). Each provider uses its own minimum score and provider-specific API parameters (`stock_images.py:542-648`).

V1 behavior includes:

- a file cache with configurable hours (`stock_images.py:404-451`);
- local JSON usage and optional direct WordPress attachment-history reuse checks;
- sequential first-provider acceptance rather than one cross-provider ranking;
- threshold, dimensions, orientation, recent asset/source URL, and download conversion checks;
- generated-image fallback after stock failure in hybrid mode (`publish_to_wp.py:1467-1509`).

## 12. Image Search V2

**IMPLEMENTED BUT DISABLED**

V2 is selected only when `IMAGE_SEARCH_ENGINE=v2` (`publish_to_wp.py:1356-1361`). It provides a common `ImageCandidate` contract with source, creator, license, dimensions, score, URL/content hashes, and attribution (`image_search/models.py:8-69`). The registry can enable Pexels, Pixabay, and Openverse (`image_search/registry.py:15-41`).

V2 behavior includes:

- concurrent provider collection, with deterministic ranking after all completions (`image_search/selection.py:144-227`);
- provider threshold normalization for cross-provider ranking (`image_search/scoring.py:102-124`);
- strict configured-license and attribution checks (`image_search/license_policy.py:36-61`);
- connected-component identity dedupe across asset key, canonical source, source page, and URL hash, retaining the highest-ranked member (`image_search/selection.py:73-80`, `image_search/selection.py:103-141`);
- exact downloaded-content and perceptual-image duplicate checks (`image_search/selection.py:93-100`, `image_search/selection.py:228-258`);
- provider failure isolation and a distinction between "all exhausted" and "provider unavailable" (`image_search/selection.py:165-186`, `image_search/selection.py:275-285`);
- generation only after confirmed search exhaustion by default (`publish_to_wp.py:1369-1385`).

Openverse requests CC0, PDM, and CC BY works and normalizes work title, creator, source page, license/version/URL, and complete attribution (`image_search/providers/openverse.py:111-205`). Tests cover provider adapters, license rejection, completion-order independence, transitive dedupe, reuse, provider failure, V1/V2 routing, and full Openverse attribution through `upload_image()` to the mocked WordPress media payload (`tests/test_image_search.py:207-1037`).

## 13. Image reuse, licensing and attribution

**CURRENT default; durable path IMPLEMENTED BUT DISABLED**

Stock reuse is tracked in a local JSON file and checked for a configurable rolling window. V2-compatible usage records include provider asset/canonical/source identities, URL/content/perceptual hashes, creator/license attribution, post ID, title, and time (`image_search/reuse.py:86-121`). That common recorder is called after a successful post for stock images in either engine (`publish_to_wp.py:1759-1763`). Consequently, a V1 run can emit `[IMG-V2] recorded image usage`; the prefix does not prove that V2 search ran.

V1 checks local usage and recent WordPress attachments. V2 adds canonical identity and downloaded-content/perceptual matching (`image_search/reuse.py:124-173`). WP-history failure is fail-open to local usage (`image_search/reuse.py:190-209`). Local file updates have no cross-process lock, so two publishers on different deployments could race even though one database advisory lock protects publishers sharing the same DB.

`upload_image()` sends selected credit/attribution text to WordPress media caption and description after upload (`publish_to_wp.py:1526-1545`). Media metadata failure is caught and does not block post publication (`publish_to_wp.py:1551-1566`). Therefore complete attribution is constructed and sent in V2, but runtime persistence is best effort rather than a publication gate.

## 14. WordPress publishing

**CURRENT**

The publisher takes the `wp_publisher_lock` advisory lock (`publish_to_wp.py:1642-1690`), counts due rich rows, and fetches up to `PUBLISH_BATCH_MAX` rows where the joined raw row is chosen, scheduled, due, and within any fresh-start cutoff (`publish_to_wp.py:226-260`, `publish_to_wp.py:683-722`, `publish_to_wp.py:1691-1704`).

The default-off legacy path preserves the Phase 1 order. With
`PUBLISH_DURABLE_STATE_ENABLED=true` after migration, the same advisory lock
wraps `PublishingService`, which claims one due rich row at a time before any
external work. For a new post it:

1. selects/generates and uploads media before creating the post;
2. ensures categories and tags through WordPress REST;
3. modifies article HTML with market/category links;
4. posts immediately with title, content, slug, current `date_gmt`, terms, and optional featured media (`publish_to_wp.py:1709-1754`);
5. on HTTP 201, writes CoinCourier identity postmeta and attempts to persist the local `wp_post_id` before image-usage and Yoast writes, then completes both `publish_status='published'` and legacy `published=1`.

The durable adapter checks a saved local `wp_post_id`, then direct WordPress postmeta for `_coincourier_publication_key`; it never uses title or slug as identity. It persists post/raw/rich/source metadata and can recover media by `_coincourier_media_publication_key` before image selection.

The durable path recovers a post when either the WordPress identity write or local ID write survives a later failure, including Yoast or local completion failure. The two writes cannot be atomic with REST creation: a hard kill immediately after HTTP 201 but before either durable write remains a narrow residual gap. The default legacy path retains its former duplicate risk until migration and explicit enablement.

## 15. Database tables and state transitions

**CURRENT - schema evidence limitation**

The checked-in dump was generated against MariaDB 10.4 in April 2025 (`crypto_news_db.sql:1-8`). It lists only basic columns plus a unique `news_url` on each table. Phase 2 adds the repository's first versioned manual migration sequence under `maintenance/migrations/`; it is not evidence that any deployed schema has been migrated.

**Runtime columns inferred from code**

| Table | Key runtime fields used by code |
|---|---|
| `cryptonewsapi` | Existing runtime fields plus additive `processing_status`, claim token/time, attempt count, and safe last error after Phase 2 migration; `processed` remains |
| `rich_crpytonews` | Existing runtime fields plus `raw_article_id`, publication claim state, `publication_key`, WP post/media IDs and metadata, external URL/timestamps after Phase 2 migration; `published` remains |
| `duplicate_assessments` | Phase 5 pair IDs, five-way classification, exact/event/lexical/fact/time evidence, reason JSON, policy version, and audit timestamps; unique per directed article/candidate/policy tuple |

```mermaid
stateDiagram-v2
    [*] --> RawStored: fetch/upsert
    RawStored --> Scored: score fields updated
    Scored --> Scheduled: chosen=1, selected_at, scheduled_for
    Scheduled --> RichStored: rich insert commits
    RichStored --> Processed: raw processed=1 commits
    Processed --> PublishDue: scheduled_for <= UTC now
    PublishDue --> WPPostCreated: WordPress HTTP 201
    WPPostCreated --> Published: WP metadata then rich published=1
    Scheduled --> Scheduled: processing failure / retry
    PublishDue --> PublishDue: pre-create failure / retry
    WPPostCreated --> PublishDue: crash before published flag (duplicate risk)
```

Before migration the rich/raw relationship remains a URL join. The additive migration backfills and uniquely indexes `raw_article_id` only after collision preflight, and adds local WordPress identity without removing legacy fields.

## 16. Scheduler, cron and advisory locks

**CURRENT**

| Stage | In-process overlap control | Cross-process control | Scope/gap |
|---|---|---|---|
| Fetch | APScheduler `max_instances=1` | MariaDB `news_fetcher_lock` | robust while lock connection lives (`fetcher.py:1146-1279`) |
| Process | chained scheduler `max_instances=1` only | none | external cron/manual calls can select the same rows concurrently |
| Publish | chained scheduler `max_instances=1` | MariaDB `wp_publisher_lock` | prevents overlap, not post-create crash duplicates (`publish_to_wp.py:1671-1790`) |

Both local and CLI chained modes isolate processing failure from publishing (`scheduler.py:46-69`; `tasks.py:43-56`). This is intentional backlog-draining behavior. No checked-in server cron definition proves whether APScheduler and cron are mutually exclusive in deployment; operators must ensure only one orchestration mode is enabled.

## 17. Failure handling and retries

**CURRENT**

| Failure | Current outcome |
|---|---|
| CryptoNews request error/non-200 | short diagnostic, empty pull fragment; cycle continues with other pulls (`fetcher.py:429-456`) |
| Fetch cycle exception | logged; lock released in `finally`; task may appear completed because exception is swallowed (`fetcher.py:1244-1279`) |
| Scoring failure | whole fetch cycle exception path; raw upserts may already be committed |
| Gemini/Grok/OpenAI transient error | bounded retries; article-level provider fallback for rewrite |
| Hard-invalid article | expansion/repair/fallback; remains retryable if all fail |
| Soft SEO failure | warning; article is stored |
| Rich commit succeeds, processed update fails | rich exists; raw remains retryable; rewrite may repeat |
| Image search/generation failure | post can proceed without featured image |
| Media metadata failure | swallowed; post proceeds, attribution may be missing |
| WP post non-201 | row remains unpublished; uploaded media can be orphaned |
| WP post created, later step fails | durable path reconciles by local ID or WP publication meta; default legacy path retains duplicate risk |

**CURRENT - fresh-start and backlog**

The fresh-start cutoff is applied to process due-count/selection (`gpt_processor.py:161-176`, `gpt_processor.py:1933-1962`) and publisher due/fetch queries through joined raw `insertDate` (`publish_to_wp.py:226-260`, `publish_to_wp.py:701-717`). Fetch and scoring do not apply it. Selection uses publication recency but does not filter `insertDate` by the cutoff (`fetcher.py:953-1048`). Thus a pre-cutoff row can theoretically consume a selected slot and then be refused by processing/publication.

The maintenance apply script clears queue fields on old, not-yet-published raw rows and deletes old unpublished rich rows, while preserving published rows and WordPress posts; it defaults to rollback (`maintenance/sql/fresh_start_apply.sql:35-66`). The dry-run's "publisher fallback" section (`fresh_start_dry_run.sql:134-149`) is stale: current `fetch_unpublished()` has no fallback after its primary query (`publish_to_wp.py:701-722`).

A rich row becomes publishable when its URL joins a chosen raw row whose schedule is due and whose rich `published` flag remains zero. It may have been prepared hours or days before the current process run, subject to cutoff. That is how publication can drain a rich backlog independently of current processing volume.

## 18. Current exact-duplicate protections

**CURRENT**

| Stage | Existing protection | Effective scope |
|---|---|---|
| Pull aggregation | exact `news_id`, cleaned URL, exact normalized-title hash | one `_pull_batch()` call only (`fetcher.py:525-549`) |
| Raw persistence | `ON DUPLICATE KEY UPDATE`; checked-in unique `news_url` | same stored source URL; other live unique keys unknown (`fetcher.py:757-797`; schema lines 110-120) |
| Scoring | candidate itself must remain unchosen for blended-score update | state guard, not duplicate detection (`fetcher.py:847-853`) |
| Selection | candidate must have `chosen_for_publish=0` | prevents reselection of one row; does not compare rows (`fetcher.py:955-963`, `fetcher.py:1031-1040`) |
| Processing | `processed=0` and `chosen=1`; rich URL upsert | retries one URL into one rich row; no process claim (`gpt_processor.py:1953-1964`, `gpt_processor.py:1717-1729`) |
| Phase 5 processor preflight | provider ID, canonical URL, normalized source hash, event ID, token Jaccard, entities, dates, numbers, actions, and publication distance | optional 72-hour selected/processed shadow window; records only and always continues |
| Publishing | rich `published=0`, joined due schedule, global advisory lock | prevents normal republish after app flag is set; not crash-idempotent (`publish_to_wp.py:683-722`, `publish_to_wp.py:1671-1775`) |
| Images | recent asset/source URL checks; V2 adds license, connected identities, SHA-256 and perceptual hash | image reuse only, not article duplicate defense |

No eligibility stage uses a body hash or compares event meaning. The disabled-by-
default Phase 5 processor hook now computes normalized source hashes and compares
deterministic event/lexical facts against recent selected or processed raw rows,
but it only records observations.

## 19. Remaining same-event/semantic duplicate gaps

**CURRENT gaps**

1. `event_id` is now requested and stored on every relevant pull, but provider
   coverage remains unmeasured and the value is shadow evidence only.
2. There is no provider-news-ID or canonical/content-hash unique constraint visible in repository DDL.
3. Conservative URL/title/content identity exists, but no new database uniqueness
   or enforcement policy uses it.
4. Multiple URLs for one event can all be scored and set `chosen_for_publish=1`.
5. Selection has no lookback across selected, processed, rich, or published rows and no topic/source quota.
6. With the source-default shadow flag off, LLM processing retains its previous
   path. With it on, deterministic analysis runs first; semantic/vector comparison
   is still absent.
7. Processing has no claim/lease, so overlapping cron/manual workers can process the same raw row.
8. Rich persistence has no raw-ID foreign key and is not atomic with `processed=1`.
9. Publication stores neither a WP post ID nor an idempotency key and does no WP reconciliation.
10. A post-success crash can create a second WordPress post on retry.
11. Logs do not connect fetch batch, raw ID, provider IDs, rich ID, source URL hash, decision, and WP ID in one record.
12. Repeated SEO focus and title framing can obscure whether repetition began in source selection or rewrite.

## 20. Findings from the attached production-style log

**CURRENT evidence**

The log begins inside a publisher run and ends inside a later processor run, so counts apply only to completed events visible in the supplied window.

| Observation | Audit result |
|---|---|
| WordPress posts | **Confirmed: 22** `Published WP post` events, IDs 5153 through 5195 (odd IDs) |
| Newly processed | **Confirmed: 10 completed attempts**, all successful: batches 5, 2, and 3; other completed runs attempted zero |
| Backlog drain | **Confirmed as the necessary explanation:** 22 posts minus 10 newly prepared rows means at least 12 posts came from rich rows prepared before the captured processing window |
| Daily selected | **Corrected:** reached 30, then 33 after additional breaking selection; reset at UTC midnight, then reached 4 |
| Semantic decisions | none logged; no `semantic`, `duplicate`, or same-event decision entries |
| Images | **Confirmed:** 12 Grok generated, 5 Pexels, 5 Pixabay; no OpenAI-generated publication image |
| Openverse | not visible |
| Search engine | V1 signatures are conclusive: `stock queries`, sequential Pexels/Pixabay messages, no V2 query/provider/ranking/license messages |
| `[IMG-V2]` | ten lines, all post-success stock usage recording; this shared recorder ran for the 5 Pexels and 5 Pixabay posts |

The ten processed raw IDs were 14559, 14561, 14608, 14598, 14508, 14626, 14590, 14712, 14744, and 14723. Their source-title failure context and SEO dumps correlate with the final ten publications (WP 5177-5195): the two ETF-flow stories, Dango, Digital X/Korbit, Mara, CLARITY odds, Iran de-escalation, Sberbank, congressional CLARITY contacts, and Fidelity. The first 12 visible publications therefore represent the prepared rich backlog in this window; this conclusion does not require assuming that any two titles are duplicates.

The 22 title/provider pairs, in publication order, were:

| WP ID | Image | Published title |
|---:|---|---|
| 5153 | Pexels | Bitcoin Dominance Reflects LTH Supply Peak |
| 5155 | Pexels | Bitcoin dominance: 3 takeaways from Strategy's credit plan |
| 5157 | Pixabay | XRP ETFs: 7 Facts Signal Institutional Shift and On-Chain Growth |
| 5159 | Pexels | Bitcoin policy focus: 5 key facets of State Dept push |
| 5161 | Grok | Bitcoin dominance: 5 key facts on the new US 'freedom tech' push |
| 5163 | Pexels | Ethereum price odds: 6 takeaways from Polymarket |
| 5165 | Grok | CLARITY Act Ethics Rules Draw Fresh Scrutiny |
| 5167 | Grok | Ripple acquisitions: 5 key audit takeaways |
| 5169 | Grok | Prediction markets: 5 trends as regulatory heat rises |
| 5171 | Pixabay | Sberbank crypto trading: 7 key details by 2026 |
| 5173 | Grok | Bitcoin Dominance Shaped by Samsung Stablecoin News |
| 5175 | Pixabay | Digital Asset Market Clarity Act gains FOP backing |
| 5177 | Grok | Bitcoin dominance in ETF flows: 3 setbacks mark 2026 H1 downturn |
| 5179 | Grok | Bitcoin Dominance Steady Despite ETF Volume Drop |
| 5181 | Grok | CLARITY Act prospects: 30% odds and 4-day Senate deadline |
| 5183 | Pixabay | Dango Shutdown Halts Trading July 29 |
| 5185 | Grok | Bitcoin Dominance Holds on Iran De-escalation |
| 5187 | Grok | Digital X Rebrands Korbit After $102M Deal |
| 5189 | Pexels | Mara Bitcoin Mining Deal Draws Lawsuit Claims |
| 5191 | Pixabay | Sberbank crypto trading plans: 3 key thresholds |
| 5193 | Grok | CLARITY Act: 5 key updates as bitcoin dominance shapes debate |
| 5195 | Grok | CLARITY Act: Fidelity backs Senate push in 2026 |

### Suspicious groups and evidence classification

Title similarity alone is not proof. The log supports the following triage, not final duplicate decisions:

| Pair/group | Log-supported classification | Why / what is missing |
|---|---|---|
| State Dept push vs US "freedom tech" push (WP 5159/5161) | **Probable same-event candidate** | published together with unusually similar policy-push framing; no raw IDs, URLs, bodies, or event IDs are logged |
| Two ETF-flow Bitcoin-dominance posts (WP 5177/5179; processed raw IDs 14559/14561) | **Probable same-event candidate or legitimate update** | distinct raw IDs; one log excerpt cites H1/June/July flows and the other low ETF volume/Ether inflows; URLs, timestamps, bodies, event IDs, and fact deltas are needed |
| Two Sberbank crypto-trading posts (WP 5171/5191; second raw ID 14712) | **Probable same-event candidate** | same distinctive bank/action/2026 launch framing within 2.5 hours; first raw identity and both source records are absent |
| CLARITY congressional-contact story raw 14744 vs Fidelity coalition story raw 14723 (WP 5193/5195) | **Broad topical overlap with a related legislative event; likely distinct factual triggers** | source titles in failure logs identify one-million contacts versus Fidelity joining a coalition; source URLs/bodies and event IDs are still needed |
| Ethics scrutiny, FOP backing, 30% odds/deadline, contacts, and Fidelity CLARITY coverage | **Same legislative topic; insufficient evidence to call exact duplicates** | titles suggest different actors, facts, and updates; event clustering should connect them while update classification decides what is publishable |
| LTH supply, Strategy credit, Samsung stablecoin, ETF flows, Iran de-escalation under "Bitcoin dominance" | **Broad topical overlap only on the log evidence** | distinct named drivers; the rewrite prompt and SEO output can add "bitcoin dominance" even when the source title did not contain it |

No pair can be classified as an **exact duplicate** from this log. Proving or rejecting that requires a joined inspection of raw ID, provider news ID, provider event ID, source URL, canonical URL, raw title/body, source publication timestamp, rich ID, WordPress ID, normalized content hash, and (for semantic triage) embedding similarity. None of that inspection was performed in this audit.

### Required publication logging

**PLANNED**

Emit one structured record per state transition, without full article bodies:

```json
{
  "event": "publication.succeeded",
  "run_id": "uuid",
  "fetch_batch_id": "opaque-id",
  "raw_article_id": 14712,
  "rich_article_id": 12345,
  "provider_news_id_hash": "sha256:...",
  "provider_event_id_hash": "sha256:...",
  "source_url_hash": "sha256:...",
  "canonical_url_hash": "sha256:...",
  "content_hash": "sha256:...",
  "title": "Sberbank crypto trading plans: 3 key thresholds",
  "source_published_at": "2026-07-25T00:00:00Z",
  "scheduled_for": "2026-07-25T23:17:17Z",
  "duplicate_mode": "shadow",
  "duplicate_decision": "allow_update",
  "matched_document_id": 456,
  "wp_post_id": 5191,
  "image_provider": "pixabay"
}
```

Provider IDs and URLs can be logged as hashes when operations do not need the clear value. Titles are already logged; bodies and embedding vectors must not be logged.

## 21. Duplicate terminology and policy

**CURRENT classifications; all actions remain planned**

| Category | Definition | Initial action |
|---|---|---|
| `exact_duplicate` | same non-empty provider news ID, conservative canonical source URL, or normalized source-content hash | record only |
| `same_event_duplicate` | same non-null event ID without new structured facts, or matching key entity plus date/number, title Jaccard >= 0.60, and bounded time | record only |
| `material_update` | same explicit/inferred event with a new date, numeric value, named participant, or action/status | record only |
| `related_event` | shared event-level entity but different date/action or insufficient same-event evidence | record only |
| `broad_topic_overlap` | only generic asset/topic overlap or no event-level identity | record only |

Event ID is a clustering signal, not an exact-duplicate key. Null IDs never match,
and different non-null IDs do not prevent deterministic inferred-event matching.
Source names are labeled evidence but do not count as key event entities. Vector
similarity remains future work and would be another signal, not a complete policy.

## 22. Recommended layered duplicate architecture

**PLANNED**

```mermaid
flowchart TD
    A[Raw candidate] --> B{Layer 1 exact identities}
    B -->|exact hit| X[Reject automatically]
    B -->|new| C{Layer 2 event and lexical cluster}
    C -->|weaker same event| Y[Shadow first, later reject]
    C -->|new/update| D{Layer 3 semantic pre-process check}
    D -->|probable same event| Z[Shadow decision and evaluation]
    D -->|allow/update/unavailable| E[LLM processing]
    E --> F{Layer 4 final publish guard}
    F -->|existing attempt/WP post/exact collision| R[Reconcile, do not create]
    F -->|safe claim| G[Create/reconcile WP draft, media, publish]
```

### Layer 1: ingestion exact checks

**PLANNED - enforce from first rollout after backfill validation**

- Store provider name plus provider news ID and enforce a scoped unique hash.
- Replace the regex-only URL identity with parser-based normalization and store both source URL and canonical URL SHA-256 values.
- Compute a normalized source-content hash after stripping boilerplate and normalizing Unicode/whitespace.
- Add unique constraints only after a collision report and deterministic survivor/backfill policy.
- Treat event ID as a non-unique cluster key.
- Preserve the rejected candidate and reason in an audit table or compact identity history rather than silently discarding all provenance.

### Layer 2: pre-selection event checks

**PLANNED - shadow, then enforce deterministic high-confidence cases**

Compare the candidate with recently selected, processed, and published rows using event ID, normalized-title token similarity, named entities, important numbers/dates, publication-time proximity, and source quality. Event clusters retain all source members but assign one primary candidate. An event-ID match alone clusters; it blocks only when corroborating facts indicate no new information.

### Layer 3: pre-processing semantic check

**PLANNED - shadow first**

Run before Gemini/rewrite cost. Query recent raw title/summary vectors first, then inspect lexical/entity/number evidence and any existing event cluster. Compare against recently selected raw stories, rich processed stories, and WordPress-linked published documents. If embeddings are missing or the hosted embedding API is unavailable, fail open to existing exact/lexical controls, enqueue retry, and continue the main pipeline.

### Layer 4: final pre-publish guard

**PLANNED - enforce deterministic idempotency immediately; semantic guard shadow first**

- Atomically claim one rich row with a lease/state transition.
- Use an application `publication_attempts` row with unique raw ID, rich ID, and deterministic idempotency key.
- Reconcile any saved WP ID before media generation.
- Query WordPress for deterministic post meta/source identity or a reserved draft when a previous response may have been lost.
- Create/reconcile a draft first, persist `wp_post_id`, then upload media and update the draft to published status.
- Recheck exact/event decisions immediately before external creation so a newly published competing worker is visible.
- Block exact identity/WP collisions automatically. Keep semantic same-event decisions in shadow until measured precision is high.

### Immediate non-vector priorities

1. Current deployed schema snapshot and collision report.
2. Apply and verify the completed durable-state migration before enabling its flags.
3. Apply the Phase 5 assessment migration and collect shadow precision evidence.
4. Measure provider event-ID population; indexing needs a deployed-schema review.
5. Build a labeled evaluation set from the persisted deterministic assessments.
6. Consider selection-time policy only after false-positive review; no current
   classification changes queue state.
7. Topic/entity rolling quotas and an explicit breaking override; do not lower `DAILY_TARGET` as a substitute.

### Topic saturation and angle diversity

**PLANNED - independent controls, shadow first**

- **Exact duplicate:** deterministic rejection is independent of all topic quotas.
- **Same event:** default to one primary article per event; allow an additional article only when classified as a material update or an audited breaking override.
- **Same topic:** start by measuring, then shadow a configurable ceiling such as three articles per six-hour rolling window and eight per 24 hours. Tune by real topic volume and editorial review.
- **Same entity:** separately shadow a configurable ceiling such as four articles per six hours, because one entity can participate in several legitimate events.
- **Breaking override:** allow a documented bypass for material urgency, but require reason, actor/policy, event cluster, and later review. A breaking flag must not bypass exact identity or publication idempotency.
- **Angle novelty:** compare source fact signatures and proposed rich summary/outline to recent cluster/topic coverage. Low novelty should request a different angle or hold for review, not silently relabel the same facts.
- **Update classification:** require a material new-information signal such as a new decision, outcome, actor, number, date, filing, exploit state, or market response. Link allowed updates to the cluster primary and prior WP post.

Calculate topic/entity saturation from both raw source labels and generated SEO labels. Keeping those dimensions separate prevents a repeated LLM focus phrase from being mistaken for repeated source selection. These controls address concentration while preserving the configured daily throughput target.

## 23. MariaDB vector architecture

**PHASE 6A, 6B1, AND 6B2 IMPLEMENTED LOCALLY; DISABLED AND NOT DEPLOYED**

Phase 6A adds an optional, separate MariaDB service and database named
`coincourier_vectors`. The reproducible local service uses `mariadb:11.8`, binds
only to `127.0.0.1:13309`, and was verified against MariaDB 11.8.9. It does not
alter or share the application database. `VECTOR_ENABLED=false` is the source
default, and no fetch, process, publish, image, or duplicate path imports the
vector store or opens its connection.

Phase 6B1 adds the embedding job engine. Phase 6B2 adds explicit lazy task
commands for bounded application-row ingestion, worker execution, and manual
historical backfill. `EMBEDDING_ENABLED=false` is also the source default. There
is no scheduler hook, automatic pipeline hook, deployment, or production provider
invocation.

At approximately 30 posts/day and 4-6 vectors/article, the mathematical range is about 43,800-65,700 vectors/year and the five-vector midpoint is 54,750. Allowing for summaries or occasional extra chunks makes 50,000-70,000 vectors/year a sound planning envelope. This is modest enough for one MariaDB deployment and an approximate vector index, while retaining relational joins and transactionally consistent metadata.

### Connector capability audit

The current dependency is unpinned `mysql-connector-python` (`requirements.txt:3`) and all DB access uses `%s` parameters (`db.py:1-5`). These vector operations are server-side SQL:

| Capability | Recommended connector use | Audit conclusion |
|---|---|---|
| `VEC_FromText()` | `INSERT ... embedding = VEC_FromText(%s)` with a JSON vector string | verified for parameterized inserts |
| `VEC_ToText()` | select `VEC_ToText(embedding)` for round-trip verification | verified through `mysql-connector-python` text results |
| `VECTOR(1536)` | fixed-dimension column plus explicit dimension/version metadata | verified practical; short/long vectors are rejected |
| distance | `VEC_DISTANCE_COSINE(column, VEC_FromText(%s))` and `VEC_DISTANCE_EUCLIDEAN(...)` | verified; cosine is the Phase 6A retrieval distance |
| `VECTOR INDEX` | `CREATE VECTOR INDEX IF NOT EXISTS name ON table (embedding) DISTANCE=cosine` | verified, including rerun and `EXPLAIN` selection |

The implementation does not bind or decode raw binary vector values. It uses
`VEC_FromText()` and `VEC_ToText()` so the connector handles ordinary text
parameters/results. Integration coverage proves DDL, inserts, round trips, cosine
ordering, dimension and malformed-input rejection, rollback, index creation, and
`EXPLAIN`. Production backup/restore rehearsal remains a deployment prerequisite.

One `VECTOR(N)` column has a fixed dimension. Phase 6A validates 1536 values in
the repository and schema metadata. A future dimension change requires a
side-by-side physical table/column and re-embedding migration, not mixed
dimensions in one vector column.

## 24. Vector schemas

**PHASE 6A STORAGE AND PHASE 6B JOB OPERATIONS IMPLEMENTED LOCALLY**

### `vector_documents`

One logical source or generated document, independent of chunks:

- `document_key`, `source_type`, durable `source_article_id`, and nullable
  `rich_article_id`;
- source URL, title, publication time, content hash/version, and timestamps;
- unique `(document_key, content_version)` for retry-safe identity.

`source_type` is `source_article` or `coincourier_generated`. A generated document
requires both its source article ID and rich article ID; a source document cannot
carry a rich ID. CoinCourier derivatives therefore remain linked to, and cannot be
treated as corroboration independent from, their factual source.

### `vector_chunks`

One active physical embedding dimension:

- document FK, deterministic chunk index, chunk text/hash, and timestamps;
- `embedding VECTOR(1536)` plus model, dimensions, and immutable version identity;
- unique document/index/version and document/hash/version keys;
- relational document/version index and cosine VECTOR index.

Keep chunk text for explainability and re-embedding only under source-retention policy. If source-content retention is restricted, retain a safe summary plus hashes and expire raw body chunks.

### `embedding_jobs`

- document FK and requested embedding version;
- `pending`, `claimed`, `completed`, `retryable`, or `failed` status;
- bounded claim token/time, attempt count, bounded last error, and timestamps;
- one row per document/version plus a status/claim-time ordering index.

Phase 6B1 implements direct job claiming, ownership checks, stale-claim recovery,
attempt counting, safe errors, reconciliation, and atomic persistence/completion.
The claim transaction commits and closes before content loading or a provider
call. Phase 6B2 implements idempotent registration/enqueue plus bounded worker and
manual backfill commands. Scheduling and production provider rollout are not
implemented.

### `duplicate_assessments`

**CURRENT after manual Phase 5 migration; disabled by source default**

- directed raw `article_id` and `candidate_article_id`;
- five-way assessment type and provider/event/URL/content equality booleans;
- title token Jaccard, publication-time distance, shared entity/date/number JSON;
- deterministic reason JSON, policy version, and created/updated timestamps;
- unique `(article_id, candidate_article_id, policy_version)` for retry-safe upsert.

It remains separate for evaluation and false-positive analysis. It contains no
vector, embedding, queue decision, enforcement mode, or full source content.

### `event_clusters` and `event_cluster_articles`

`event_clusters` stores cluster key, primary raw article, lifecycle, first/last seen, and optional canonical event summary. The member table stores raw article, relationship (`primary`, `duplicate`, `update`, `related`), similarities, new-information score, and decision time. These are separate because clusters are many-to-many over time and primary membership can change.

### `rag_retrieval_audit`

Stores article, retrieved document/chunk, similarity, rank, purpose (`duplicate`, `context`, `internal_link`, `angle`), policy version, and time. It can be deferred until retrieval features begin and pruned/partitioned because it will grow faster than documents.

### `publication_attempts` (non-vector but critical)

Add `id`, unique idempotency key/raw ID/rich ID, `wp_post_id`, deterministic WP slug/meta key, state, lease, attempt count, last error, and timestamps. This table should precede semantic enforcement because it closes the confirmed crash duplicate window.

The proposed tables should not be collapsed into the two article tables. Document/chunk, asynchronous job, decision audit, event membership, retrieval audit, and external publication have different cardinality, retention, and concurrency requirements.

## 25. Embedding and chunking strategy

**PHASE 6B1 AND 6B2 IMPLEMENTED LOCALLY; DISABLED AND NOT DEPLOYED**

### Phase 6B1 embedding decision

The approved baseline is OpenAI `text-embedding-3-small`, 1536 dimensions, cosine
distance, deterministic `chunk-v1`, and immutable embedding identity
`openai:text-embedding-3-small:1536:chunk-v1`. The implementation provides a
provider protocol, deterministic fake provider, and mockable OpenAI adapter. The
adapter sends explicit `model`, `input`, and `dimensions`; no live embedding smoke
or production call is part of Phase 6B1. A later model or dimension must be
evaluated side by side rather than silently replacing existing vectors.

### Document and chunk policy

`chunk-v1` normalizes title plus visible body with Unicode NFKC, CRLF
normalization, horizontal-whitespace collapse, and paragraph preservation. HTML
is reduced to visible text; script/style content is ignored. Numbers, dates,
tickers, names, percentages, currency, and negation remain.

Segmentation is paragraph/sentence first, using a deterministic stdlib
word/punctuation token estimate. It targets 500 token units, flushes a current
chunk at or above 350 when the next segment would exceed the target, never exceeds
600, uses zero overlap, and splits oversized segments only at whitespace so words
are not broken. The SHA-256 content hash covers exact normalized title plus body;
`content_version` is `chunk-v1:<content-hash>`. Each exact normalized chunk has a
SHA-256 hash and deterministic contiguous index. Exact duplicate chunks within a
document are retained once.

Source and CoinCourier-generated provenance remains separate in
`vector_documents`. Phase 6B2 maps raw `cryptonewsapi.id`, title, and `full_text`
to `source_article` documents. It maps `rich_crpytonews.id`, title, and
`full_text` to `coincourier_generated` only when the durable `raw_article_id`
join resolves; missing lineage is skipped. Registration uses normalized title
plus visible body and never treats URL alone as identity.

### Retention and re-embedding

- Keep published document metadata and vectors while posts remain live.
- Keep recent raw candidates long enough for duplicate/event windows; propose 90 days for rejected/unselected metadata and 12-24 months for source chunks, subject to provider/license policy.
- Keep hashes and cluster/audit decisions longer than source text when legally permitted.
- Re-embed side by side into a new dimension-compatible physical table/version, validate coverage and retrieval quality, switch reads, then retire the old version after rollback expiry.
- Backfill newest published/raw documents first through the manual bounded task;
  restart safety comes from descending keyset scans and deterministic documents/jobs.

### Async failure policy

Phase 6B1 claims one exact embedding version with an opaque ownership token,
commits and closes the claim transaction, then loads content and calls the
provider. Complete valid persisted chunks reconcile the job with zero provider
calls. Otherwise vectors are generated in bounded batches and full replacement
plus completion occurs atomically in a separate short transaction. Invalid
content identity, configuration, or provider output is terminal. Genuine
provider/network unavailability is retryable. Lost ownership is distinct.
Unexpected storage/programming faults release the claim to a recoverable state
when possible and propagate; if cleanup itself is unavailable, normal claim
expiry still permits recovery.

Phase 6B2 adds `embedding_ingest`, `embedding_worker`, and
`embedding_backfill source|generated`. The worker requires both enable flags,
preflights the approved OpenAI/model/dimension/chunker contract, API key, and
both database connections before claiming, then processes a bounded job count.
Provider requests retain the Phase 6B1 batch size; a 100-chunk per-job cap makes
paid work finite and rejects oversized input before a call. Retryable provider
results end the current run to avoid immediate hot reclaim. Across later runs,
pending jobs rank first and retryable jobs rotate by least attempts and oldest
retry update, preventing one repeatedly failing newest job from starving other
ready jobs. Failed, lost-claim, and reconciled jobs are counted separately. A
genuine empty queue is success.

Backfill takes a fixed per-run high-water ID and scans descending keyset pages.
It deliberately persists no extra cursor table: immutable content versions and
unique document/job constraints make restarts idempotent, while rescanning also
finds historical edits and rows inserted after an earlier high-water mark. This
trades repeated reads for no Phase 6B2 migration. Claims order by publication
time newest first, so historical rows registered later do not overtake fresh
dated articles. Backfill remains a separate manual invocation.

Each operation logs count-only run metrics: scanned/registered/skipped documents,
enqueued/existing jobs, claimed/completed/reconciled/retryable/failed/lost jobs,
provider calls, embedded chunks, and token usage when supplied. No article text
or vector is logged. External cron may eventually run `embedding_ingest` then
`embedding_worker` every few minutes; no scheduler or deployment change exists.
Phase 6C1 now provides isolated semantic retrieval and offline evaluation.
Automatic duplicate-shadow evaluation remains Phase 6C2 and must not be inferred
from Phase 6B storage, embedding completion, or a retrieved neighbor.

## 26. Retrieval use cases

**PHASE 6C1 SOURCE-ARTICLE RETRIEVAL IMPLEMENTED LOCALLY; OTHER USES PLANNED**

Phase 6C1 retrieves distinct historical source articles for one already embedded
source article. The newest immutable vector document must have a completed job
and nonempty chunks for the exact requested embedding version. Query publication
time is required. Candidates must be non-null dated source articles in the
inclusive `[query time - 72 hours, query time]` window; the same document and all
versions sharing the query source ID are excluded. Generated CoinCourier vectors
cannot corroborate duplicate/event evidence.

At most eight query chunks are sampled deterministically across the document.
Each performs a native cosine ANN query with 5x article oversampling, capped at
100 chunk rows. Article top-K defaults to 10 and cannot exceed 20. Results group
by durable source article ID and rank by the best/minimum native chunk distance,
with chunk indexes, document/source IDs, publication delta, version, and source
type retained as evidence. No full vectors or complete chunk text are returned.

1. **Semantic duplicate retrieval:** Phase 6C1 implements source-only candidates and distance evidence; event-feature reranking and decisions remain planned.
2. **Historical factual context:** retrieve raw-source chunks separately and label their source/time. Generated CoinCourier text can provide continuity but not corroboration.
3. **Internal-link suggestions:** retrieve published CoinCourier chunks, require live WP IDs/URLs and topical relevance, and avoid self-links.
4. **Repetitive-angle avoidance:** compare proposed rich summary/outline to recent published derivatives in the same event/topic, then prompt for genuinely new facts or framing.
5. **Update classification:** compare fact signatures and source chunks to determine whether a new article contains material changes.

Future retrieval output must retain provenance appropriate to its purpose. A
vector result alone must never be presented as factual confirmation.

## 27. Shadow-mode rollout

**PHASE 6C1 OFFLINE/DISABLED; PHASE 6C2 SHADOW INTEGRATION PLANNED**

Current and future switches:

```text
VECTOR_ENABLED=false
EMBEDDING_ENABLED=false
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
EMBEDDING_CHUNKER_VERSION=chunk-v1
EMBEDDING_BATCH_SIZE=16
EMBEDDING_INGEST_LIMIT=25
EMBEDDING_WORK_LIMIT=5
EMBEDDING_CLAIM_TIMEOUT_MINUTES=30
EMBEDDING_BACKFILL_PAGE_SIZE=100
EMBEDDING_MAX_CHUNKS_PER_JOB=100
SEMANTIC_SHADOW_ENABLED=false
SEMANTIC_LOOKBACK_HOURS=72
SEMANTIC_TOP_K=10
VECTOR_DUPLICATE_MODE=off
```

The vector and embedding settings exist with disabled source defaults. The
Phase 6C1 semantic flag is also false and is read only by direct package use.
Explicit embedding task commands read the embedding flags; no automatic fetch,
process, publish, duplicate, or scheduler path invokes semantic retrieval.

The proposed future `VECTOR_DUPLICATE_MODE` contract is `off|shadow|enforce`:

- `off`: schemas/jobs may exist, but no duplicate retrieval decision affects or logs candidate comparisons.
- `shadow`: generate/query vectors and persist assessments, but never reject or delay the article.
- `enforce`: only policy-approved, evaluated high-confidence decisions can block; embedding unavailability still fails open.

Rollout order is exact constraints/idempotency first, event and lexical shadow next, vector backfill, semantic shadow for several weeks, human labeling, then limited enforcement. Breaking-news overrides must be explicit and audited, not an unlogged bypass.

## 28. Metrics and evaluation

**PHASE 6B2 RUN COUNTS AND PHASE 6C1 OFFLINE EVALUATION IMPLEMENTED LOCALLY**

The explicit ingestion, worker, and backfill tasks emit bounded count-only run
summaries. Phase 6C1 loads a versioned synthetic JSON relationship fixture and
reports candidate rank, Recall@K, first-relevant MRR, per-label native-distance
distributions, top-K labeled coverage, missing pairs, and unavailable queries.
Strict duplicate relevance means exact plus same-event duplicate; broader
same-event relevance additionally includes material updates. Unavailable queries
are reported and excluded from Recall/MRR and coverage denominators, while a
valid no-candidates result remains an evaluated retrieval miss. Real labels,
dashboards, production alerts, and threshold calibration remain planned.

Build a labeled set containing exact duplicates, same-event duplicates, legitimate updates, and broad topical overlap. Include the suspicious log groups but label them only after source/DB inspection. Evaluate at pair and publication-decision level.

Track:

- exact collisions by identity type and source;
- candidates clustered, selected, rejected, overridden, and later published;
- duplicate precision/recall and false-positive rate by policy version;
- update recall and incorrect update suppression;
- topic/entity publications per rolling window;
- LLM calls/cost avoided before rewrite;
- embedding job latency, retry/dead rate, and active-version coverage;
- vector query latency and candidate count;
- publication claims recovered, reconciled WP posts, orphaned media, and duplicate WP incidents;
- operator review/override outcomes.

Initial similarity hypotheses, to be calibrated on real labels:

| Use | Starting hypothesis | Required corroboration/action |
|---|---|---|
| Exact duplicate | deterministic identity/hash, no vector threshold | enforce automatically after collision backfill |
| Probable same event | cosine >= 0.92 on raw summaries | require close time plus entity/event/number evidence; shadow first |
| Related context | cosine 0.78-0.92 | retrieval only; never block |
| Internal link | cosine >= 0.80 on published chunks | live WP target, editorial/topic checks |
| Repetitive angle | cosine >= 0.86 on rich summaries/outlines | same topic/event window; prompt/review before any blocking |

These values are hypotheses, not universal truths. Model version, text type, language, chunk type, and corpus distribution change score meaning. Event ID, lexical overlap, entities, numbers, dates, and new-information scoring must remain separate features.

Phase 6C1 installs none of these hypotheses as configuration or policy. It exposes
native MariaDB cosine distance only and contains no suppression threshold.

## 29. Security and secret handling

**CURRENT and PLANNED**

- Continue sourcing credentials from environment variables (`config.py:18-205`); never persist them in docs, vector metadata, logs, or assessment reasons.
- Pin and scan dependencies before vector rollout; current requirements are unpinned.
- Redact provider tokens and credential-shaped strings from every error path, not only existing Grok/OpenAI helper errors (`gpt_processor.py:503-511`; `publish_to_wp.py:898-905`).
- Restrict `GET /api/news` or return an explicit public projection before treating Flask as internet-facing.
- Treat source chunks and embeddings as sensitive derived data. Approve hosted-provider retention/training terms and source-content licensing before backfill.
- Encrypt DB transport with certificate verification in production; current app DB config sets `ssl_verify_cert=False` (`config.py:187-196`).
- Log hashes and IDs rather than full URLs where practical, never full bodies or vectors.
- Limit WP application-password and direct-DB privileges. Publication reconciliation should need only the minimum post/meta access.

## 30. Deployment and migration roadmap

**PLANNED**

Each phase is intentionally deployable and reversible on its own.

### Phase 0: documentation and baseline measurements

- Files: this document, future log parser/metrics documentation.
- Migration: none.
- Tests: fixture-based parser against a redacted copy of the supplied log; baseline title-pair labeling format.
- Logs/metrics: current throughput, retries, backlog age, topic counts, duplicate incidents, post-create failures.
- Switch: none.
- Rollback: remove dashboards/parsers without runtime effect.
- Risk: low.

### Phase 1: exact dedupe and publication idempotency

- Files: likely `fetcher.py`, `gpt_processor.py`, `publish_to_wp.py`, `config.py`, DB helper/repository modules, and new focused tests.
- Migration: canonical/content/provider-ID hashes; current schema reconciliation; processing claim fields; `publication_attempts`; raw/rich/WP identity links and validated unique indexes.
- Tests: URL/content normalization, constraint collisions, concurrent process claims, lost WP response, crash after WP 201, WP reconciliation, orphan-media cleanup.
- Logs/metrics: exact reason, idempotency key, claim/reconcile states, raw/rich/WP IDs.
- Switch: `EXACT_DEDUPE_MODE=shadow|enforce`, `PUBLICATION_IDEMPOTENCY_ENABLED` defaulting on only after backfill.
- Rollback: disable exact enforcement while retaining identities; publication code reads legacy rows and existing `published` flag.
- Risk: medium-high because it touches external side-effect ordering; highest priority.

### Phase 2: CryptoNews event-ID ingestion and event clustering

- Status: event-ID request coverage and pairwise shadow evidence are implemented by
  project Phase 5; durable event-cluster tables and primary-member policy remain planned.
- Files: `fetcher.py`, config, deterministic policy/repository, tests.
- Migration: provider-scoped event ID index plus `event_clusters` and `event_cluster_articles`.
- Tests: every pull requests event ID, missing IDs, reused IDs, multiple sources per event, legitimate updates.
- Logs/metrics: event-ID coverage, cluster size, primary changes, source diversity.
- Switch: `EVENT_CLUSTER_MODE=off|shadow` initially.
- Rollback: stop cluster writes/reads; keep additive data.
- Risk: medium because provider event semantics/coverage are unverified.

### Phase 3: lexical/entity duplicate checks in shadow mode

- Status: implemented but disabled before processor LLM work; selection integration
  and all enforcement remain planned.
- Files: normalization/fact/policy modules; processor preflight; focused tests.
- Migration: pairwise assessment rows only; existing raw identity columns are reused.
- Tests: punctuation/Unicode/title variants, entity and number/date deltas, update versus duplicate fixtures.
- Logs/metrics: component scores and shadow decision reasons, without bodies.
- Switch: `DUPLICATE_SHADOW_ENABLED=false`, with lookback and policy version controls.
- Rollback: switch off; no queue state changes in shadow.
- Risk: low-medium.

### Implementation Phase 6A/6B: vector storage, then embedding jobs

- Phase 6A status: local storage foundation implemented, disabled, and not deployed.
- Phase 6A files: separate config/connection/repository, independent vector
  migrations, local MariaDB 11.8 compose service, and synthetic integration tests.
- Phase 6A migration: `vector_documents`, fixed-dimension `vector_chunks`,
  storage-only `embedding_jobs`, relational keys, and cosine VECTOR index.
- Phase 6A tests: disposable MariaDB 11.8.9 DDL/functions/index/query plans,
  dimensions, malformed values, transactions, idempotency, and provenance filters.
- Phase 6B1 status: deterministic `chunk-v1`, provider abstraction, fake provider,
  OpenAI adapter, versioned settings, direct job claims/recovery, provider-outside-
  transaction execution, atomic completion, and paid-call reconciliation are
  implemented locally and verified on MariaDB 11.8.
- Phase 6B2 status: application document ingestion, idempotent job enqueue,
  bounded worker/manual backfill task commands, preflight controls, and count-only
  metrics are implemented locally. They are disabled and not deployed.
- Switches: `VECTOR_ENABLED=false` and `EMBEDDING_ENABLED=false`; only explicit
  embedding task commands read them. No automatic pipeline path reads either one.
- Eventual external cron sequence: run `embedding_ingest`, then
  `embedding_worker`, every few minutes. Keep `embedding_backfill` manual and
  separately bounded. APScheduler and deployment configuration remain unchanged.
- Rollback: leave the separate service absent/disabled; the application DB and
  deterministic pipeline remain independent.

### Implementation Phase 6C1: semantic retrieval and evaluation

- Status: bounded source-only retrieval and synthetic labeled evaluation are
  implemented locally, offline, disabled, and verified on MariaDB 11.8.
- Migration: none. Phase 6C1 reads existing vector documents/chunks/jobs only.
- Tests: exact-version readiness, causal 72-hour filtering, source/generated
  provenance, source-identity exclusion, distinct article aggregation, native
  cosine ordering/index plan, bounded work, Recall@K/MRR, and availability rules.
- Switch: `SEMANTIC_SHADOW_ENABLED=false`; there is no automatic task or pipeline
  hook, threshold, decision, or durable semantic assessment.
- Rollback: leave the switch false or remove the isolated package; existing
  deterministic and publication behavior is independent.

### Phase 6C2: semantic duplicate shadow mode

- Files: semantic retrieval/reranking service; selection/processor preflight hooks; audit repository; tests.
- Migration: `duplicate_assessments`; possibly retrieval-audit table.
- Tests: labeled exact/event/update/topic corpus, missing embeddings, provider outage fail-open, model-version isolation.
- Logs/metrics: top match and all feature scores, policy version, shadow decision, human outcome.
- Switch: `VECTOR_DUPLICATE_MODE=shadow`.
- Rollback: `off`; no publication decisions were blocked.
- Risk: medium.

### Phase 6: enforce high-confidence duplicate blocking

- Files: policy engine, override/admin workflow, selection/processor integration, tests.
- Migration: decision/override metadata and cluster winner constraints if needed.
- Tests: false-positive regressions, breaking override, concurrent candidates, fail-open outage, appeal/requeue.
- Logs/metrics: block precision target, overrides, missed duplicates, update suppression.
- Switch: `VECTOR_DUPLICATE_MODE=enforce` for a narrow policy/version and cohort.
- Rollback: return to `shadow`; requeue blocked rows with preserved state.
- Risk: high; require measured precision and editorial sign-off.

### Phase 7: historical context and internal-link retrieval

- Files: retrieval service, prompt/context builder, link validator, publisher integration, tests.
- Migration: `rag_retrieval_audit`; published URL/status fields.
- Tests: source/generated provenance separation, stale/deleted WP targets, self-link and hallucinated-link prevention.
- Logs/metrics: accepted suggestions, click/quality review, retrieval provenance and latency.
- Switch: separate `RAG_CONTEXT_MODE` and `INTERNAL_LINK_MODE`, both shadow/suggest first.
- Rollback: disable context/link injection; articles continue without retrieval.
- Risk: medium.

### Phase 8: angle diversity and topic-saturation policy

- Files: topic/entity counters, angle classifier/policy, selection and rewrite integration, tests.
- Migration: optional topic assignments and rolling-decision audit; derive counters rather than storing mutable totals where practical.
- Tests: rolling windows, entity/topic distinction, breaking override, legitimate update, timezone boundaries.
- Logs/metrics: topic/entity frequency, angle novelty, overrides, diversity distribution.
- Switch: `TOPIC_SATURATION_MODE=off|shadow|enforce` and independent angle mode.
- Rollback: shadow/off; exact/event defenses remain.
- Risk: medium-high because editorial variety is policy-sensitive.

### Phase 9: database-backed image budgets and publisher crash recovery

- Files: image budget/reuse repository, publisher outbox/recovery worker, cleanup task, tests.
- Migration: image generation budget/usage and media/publication attempt state, including orphan tracking.
- Tests: multi-instance budget races, crash at every media/post/meta transition, stock attribution retry, orphan cleanup.
- Logs/metrics: daily provider budget, media/post reconciliation, orphan count/age, attribution completion.
- Switch: DB budget and recovery switches with local-file fallback during rollout.
- Rollback: disable budget/recovery workers; preserve publication idempotency and existing usage JSON compatibility.
- Risk: medium-high.

## 31. Testing strategy

**CURRENT**

The only checked-in test module is image-search focused. It has strong mocked coverage for V2 provider normalization, license policy, deterministic/transitive ranking, download/reuse behavior, generation gating, V1 rollback, and Openverse-to-WordPress attribution. There are no checked-in tests for fetch ingestion identities, selection duplication, process concurrency, rich transactions, fresh-start consistency, publisher idempotency, or backlog ordering.

**PLANNED**

- Unit-test URL/title/content normalization and policy feature extraction.
- Use table-driven duplicate fixtures for all four terminology categories.
- Add DB integration tests against disposable MariaDB 11.8, including unique collisions, transactions, advisory locks, claim leases, vector functions/indexes, and migrations.
- Add mocked CryptoNews payload tests with present/missing/reused `eventid`.
- Add processor tests proving a duplicate guard runs before Gemini/rewrite calls.
- Add WordPress contract tests for draft reservation, lost responses, existing-meta reconciliation, post ID persistence, media attribution, and publish retry.
- Add concurrency tests with two process/publish workers.
- Replay a redacted log fixture to validate counts and correlation fields.
- Build a human-labeled offline retrieval set and pin model/policy versions in expected results.
- Test every rollout switch in off, shadow, enforce, outage, and rollback modes.

No test should make a live provider, WordPress, or production DB call by default.

## 32. Rollback plan

**PLANNED**

1. Keep V1 image search as the default throughout duplicate/vector work.
2. Make all new schemas additive and old fields readable until the rollback window closes.
3. Separate exact/idempotency enforcement switches from event, lexical, vector, topic, RAG, and angle switches.
4. Roll semantic enforcement back to shadow without deleting assessments or vectors.
5. Preserve old embedding tables/version during side-by-side model/dimension rollout.
6. Requeue falsely blocked rows from explicit decision state; never reconstruct state from logs alone.
7. Retain deterministic publication attempts and WP IDs even if other new features are disabled; disabling idempotency after it owns posts would reintroduce duplicate risk.
8. Back up and verify restore before DDL; migration rollback must account for new rows created while dual-write is active.
9. Keep V1/V2 image usage fields mutually readable during image-budget migration (`image_search/reuse.py:86-121`).

## 33. Operational runbook

**CURRENT safe checks**

- Confirm exactly one orchestration mode: external cron or local APScheduler.
- Check `news_fetcher_lock` and `wp_publisher_lock` holders before diagnosing a skipped run.
- Compare selected-unprocessed, processed-without-published-rich, unpublished rich, and publish-ready counts using the reviewed dry-run queries; do not run the apply script without replacing the cutoff and changing its default rollback intentionally.
- Treat "processing succeeded" and "publishing succeeded" as stage summaries, not proof that every intended row was handled.
- For a suspected duplicate, collect raw/rich/WP IDs, provider news/event IDs, source/canonical URLs, source timestamps, content hashes, and decision records before deleting or unpublishing anything.
- For a post visible in WordPress but `published=0`, enable no new behavior until Phase 2 migrations are verified. Once the durable path owns the row, reconcile only by saved WP ID or CoinCourier publication key, never title/slug.

**IMPLEMENTED LOCALLY BUT DISABLED vector-job checks; IMPLEMENTED BUT DISABLED idempotency checks**

- Monitor active embedding version coverage and failed/retryable jobs using the
  count-only ingest/worker metrics; do not log body or vector values.
- Verify both enable flags, vector DB isolation, API key, approved provider/model,
  1536 dimensions, and `chunk-v1` agreement before running a worker.
- Run fresh ingestion before workers. Invoke source/generated backfill separately
  with small limits so it remains operationally subordinate to fresh work.
- Inspect exact identity and cluster evidence before overriding a decision.
- When embeddings are unavailable, confirm semantic checks show `unavailable/fail_open` while exact and publication-idempotency controls remain active.
- Reconcile stuck publication leases by deterministic idempotency key and WP post ID; never create a new post until reconciliation proves none exists.
- Audit media attribution completion for V2/Openverse and retry metadata separately from post creation.

## 34. Future improvements

**OPTIONAL FUTURE**

- A small editorial review UI for clusters, updates, overrides, and labeled examples.
- Source-quality calibration learned from corrections rather than static priors alone.
- Multilingual/cross-lingual duplicate evaluation if source languages expand.
- Claim-level fact extraction and contradiction detection for update classification.
- Time-decayed entity/event graphs for longer historical narratives.
- Automatic internal-link maintenance when WordPress slugs or post status change.
- Cluster-aware newsletter/homepage diversity and not only publication eligibility.
- A formal outbox for all external side effects, including taxonomy and media cleanup.
- OpenTelemetry traces and metrics with run/raw/rich/WP correlation IDs.
- A current generated schema snapshot and migration tool with CI-tested forward/rollback paths.

## 35. Open questions and decisions required

**PLANNED decisions**

1. What is the actual application DB server/version, and does production run MariaDB 11.8 with native vector support?
2. What is the complete current DDL and index set for both article tables?
3. How often is CryptoNews `eventid` populated, how stable is it across sources/updates, and is it scoped globally or by feed/provider?
4. Which canonical URL rules and source-content retention terms are approved?
5. Which suspicious log pairs are duplicates, updates, or merely related after source/DB review?
6. What false-positive ceiling and editorial override workflow are acceptable before semantic enforcement?
7. Should WordPress idempotency use a registered REST meta field/custom endpoint, direct DB reconciliation, or both?
8. Is draft-first post reservation acceptable operationally, and how long may stuck drafts/media remain?
9. What provider data-retention policy must be enforced before production source-text embedding is enabled?
10. What quality/cost benchmark and rollback window are required before replacing the approved 1536-dimension baseline?
11. What rolling windows and caps apply independently to events, topics, entities, sources, and breaking news?
12. Should the hard-coded `bitcoin dominance` SEO examples be replaced with neutral/dynamic examples to reduce framing repetition?
13. Should generated `schema_jsonld` and `image_alt` be persisted, and must missing licensed-image attribution block publication?
14. Which external cron configuration is authoritative, and can all processing workers share a DB-backed claim protocol?
15. When should the stale schema dump and stale dry-run "publisher fallback" section be replaced with generated, versioned documentation?
