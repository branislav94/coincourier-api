# Conservative same-event duplicate enforcement

## Audit and root cause

Audited against `origin/dev` at `c541e599035ec4263f225f3074be6f5f0f739729`.
The existing controls were deliberately observational:

- `duplicate_detection/shadow.py:analyze_duplicates_in_shadow` classified and
  persisted pairs, then logged `decision=continue`.
- `gpt_processor.py:_run_duplicate_shadow_fail_open` invoked that hook;
  `process_one` continued into enrichment and rewriting regardless of its result.
- `duplicate_detection/policy.py:assess_relationship` already produced
  `exact_duplicate`, `same_event_duplicate`, `material_update`, `related_event`
  and `broad_topic_overlap`. There was no enforcement mode.
- `semantic_retrieval/shadow.py:SemanticShadowRunner.run` persisted retrieval
  evidence but did not expose the candidates to the processing decision.
  `semantic_retrieval/service.py` resolves existing source documents/chunks from
  the vector store; it does not establish event equality from distance.
- `fetcher.py` stores upstream CryptoNewsAPI `eventid` as
  `cryptonewsapi.event_id`; `news_id` identifies the individual provider article.
  `source_name` is the reporting outlet, not a provider identity namespace.
- `repositories/raw_news.py:RawNewsRepository.claim_next` owns raw processing;
  `gpt_processor.py:store_rich_news` creates the rich article.
- `publishing/service.py:PublishingService._publish_claim` reconciled an
  article's own WordPress identity before preparing media and publishing.
  `publishing/wordpress/publisher.py:WordPressPublisher.publish` guarded retries
  of that identity, but not a second rich article about the same event.

## Policy and early disposition

`DUPLICATE_ENFORCEMENT_MODE` accepts `off`, `shadow` and `enforce`, defaults to
`shadow`, and rejects invalid or explicitly empty values. `off` skips the
deterministic duplicate hook. Both `shadow` and `enforce` call
`assess_duplicate_enforcement` before enrichment and again when semantic source
candidates expand coverage. Both also call it before media and immediately
before WordPress creation. The old optional shadow hook runs first so its
legacy audit cannot overwrite the new policy evidence.

The engine computes classification and confirmation without branching on mode.
Shadow persists/logs `would_reject_duplicate` or `would_continue`, then processing
and publication continue. Only enforce applies terminal rejection. Shadow
never disposes a claim, defers publication or takes the publication mutex.
`DUPLICATE_SHADOW_ENABLED` does not gate the new shared policy; semantic flags
retain their own meaning. Shadow evaluation/audit failures are sanitized and
nonblocking.

`duplicate_detection/enforcement.py:assess_duplicate_enforcement` reloads the
original raw source and fresh candidate rows. Only credible rich-backed prior
articles can suppress raw processing; merely selected, unprocessed peers and
terminal duplicates cannot. Published candidates are preferred as canonical,
then the earliest credible primary. Reads remain bounded by the existing
lookback (default 72 hours) and 200 candidates; supplied semantic source IDs can
extend the date filter within that bound.

A hard rejection requires no extracted new facts and no evidence uncertainty,
plus either:

1. `exact_duplicate` with an identical normalized source-content fingerprint
   (at least 80 characters); URL or provider-article equality alone is insufficient.
2. `same_event_duplicate`, a valid matching upstream event ID,
   `no_new_structured_facts`, a shared non-outlet entity, and a shared concrete
   action, date, value or specific product/protocol anchor.
3. `same_event_duplicate` confirmed by the conservative structured cross-source
   signature described below, even when upstream event IDs differ or are absent.

Title similarity is audit evidence, never sufficient blocking evidence.
Placeholder event IDs are ignored. An upstream event ID can span reporting
outlets, but different IDs can also describe the same real event. Lexically
inferred same-event pairs without a confirmed structured signature remain
observational unless identical source content independently proves exactness.

## Structured cross-source confirmation

The live FOMC miss exposed two gaps in the first implementation: the new engine
was called only in enforce mode, and confirmation required equal provider event
IDs. The old small vocabulary also did not normalize Fed/FOMC rate-hold language
into an explicit decision signature. A reported `broad_topic_overlap` therefore
continued even though the supplied event facts matched.

Provider-event identity remains the strongest path. The additional path uses
only original raw source/title facts. It requires the same supported event kind,
explicit primary entity/actor, action/outcome, one full calendar event date, and at
least two distinct strong corroborating anchors. There must be no extracted
material-new fact, contradictory fact or unresolved evidence. Neither vector
distance nor title similarity contributes confirmation evidence.

Supported signatures cover the FOMC held-rate decision, JPMorgan/Solana fund
launch, Ethereum Hoodi/Glamsterdam postponement, Solana Alpenglow mainnet
activation, and Bitcoin Core AI-vulnerability security-policy revision. Anchors
describe the actual event: target range, vote/result, named meeting, specific
product/action target, mainnet/testnet milestone or AI-policy context. Reporting
outlets and generic crypto mentions are excluded. Mere publication-time proximity
does not replace the explicit event date. Unsupported or sparse cases continue.

`tests.test_duplicate_cross_source.fomc_source_pair` reconstructs raw-source
wording from the supplied DEV facts for IDs `18789`/`18825`; it does not read a
production database or compare generated articles. Its source bodies differ.
Before this follow-up, the fixture classified as `related_event`, with no
confirmation; unnormalized range/vote formats also produced a spurious numeric
delta. (The supplied live log classified its actual pair as
`broad_topic_overlap`.) Afterward, the fixture is `same_event_duplicate`,
`same_event_id=false`, `event_identity_source=structured_cross_source`,
`confirmed_duplicate=true`, with shared `2026-10-28`, `rate_range:3.75%-4%` and
`vote:10-2`, no new-fact/contradiction/uncertainty flags, and title Jaccard
`0.13333`. Shadow records `would_reject_duplicate`; enforce rejects.

Different dates, rate ranges, votes, actions, targets or outcomes veto a block.
New guidance, forecasts, press-conference outcomes, actors, filings or subsequent
market results remain updates or uncertain evidence. Equivalent range/vote
formatting is normalized; unresolved current/prior value and date roles still
fail open. These checks also apply to the provider-ID path.
An explicit successful activation after a postponement is a material update;
the noun describing a delayed activation is not a completed outcome. Client
release and activation targets remain distinct even with matching provider IDs.
Named funds and specific policy-rule targets replace generic category anchors
when present, preventing different launches/revisions from confirming each other.

The classifier checks fact deltas before mutable URL/article identities.
New dates, numbers, named participants, references, actions or status preserve
`material_update`. Different reporting dates and regulatory actions remain
publishable. Negated/speculative outcomes, unresolved date/value roles and
ambiguous participant roles fail open. Extraction is intentionally conservative
and is not a complete language-understanding system: uncertain or unrecognized
cases may still publish and remain available for audit.

In enforce mode, `gpt_processor.py:process_one` checks before semantic retrieval,
enrichment, rewrite, repair, rich storage or image selection. It returns a typed
`ProcessingDuplicate` outcome. Durable processing uses the claim token to call
`RawNewsRepository.reject_duplicate`; legacy processing may only dispose an
unclaimed row. The terminal state is `processing_status='duplicate'`,
`processed=1`, `chosen_for_publish=0`, no schedule or claim, and no failure error.
Duplicate outcomes have their own counter and do not become successful rewrites
or retryable processing failures. Failed ownership/persistence does not pretend
that disposition succeeded.

## Semantic evidence and audit

The semantic runner now returns immutable, validated source candidate IDs even
when writing vector-side audit evidence fails. The enforcement engine reloads
those sources from the application database and applies exactly the same
deterministic policy. Native distance is recorded, not thresholded into a block.
There is no inline embedding request, new model or dimension change. Existing
completed vectors can still be used when embedding generation is disabled.
Missing vectors or embedding outages leave deterministic enforcement available.

Before rejecting, the engine must persist its assessment. Existing
`duplicate_assessments.reason_json` records mode, stage, final decision,
canonical raw/rich/WP identity and URL, provider/event identities, policy version,
title similarity, shared entities/actions, fact deltas, uncertainty and optional
semantic distance. New fields include `event_identity_source`
(`provider_event_id`, `structured_cross_source`, `none`), `confirmed_duplicate`,
`confirmation_anchors`, `new_fact_flags` and `contradiction_flags`. Logs contain
structured evidence, not article bodies or
exception details. Application-database/audit failure fails open during raw
processing; publication must recheck before any new external post.

Per-article counters include `exact_blocks`, `same_event_blocks`,
`material_updates_allowed`, `related_events_allowed`,
`broad_topic_overlaps_allowed`, `semantic_candidates_evaluated`,
`semantic_candidates_promoted`, `fail_open_unavailable_embeddings`,
`shadow_would_reject_duplicates` and `cross_source_duplicates_confirmed`.
The last counts actual unavailable retrieval, not disabled retrieval or an empty
successful search. Pair classifications count assessed pairs; block counters
count the final article decision. Shadow summaries keep actual block/rejection
counters at zero; would-reject and cross-source confirmation counts remain
separately observable.

## Final publication boundary

`duplicate_detection/publication.py:PublicationDuplicateGuard` reuses the
application-database advisory-lock helper with conflict group
`duplicate-publication`. It serializes new publication globally, including
direct service callers that bypass the normal one-shot job lock. A single
global group also covers identical content with different or missing event IDs.
It holds a connection-bound mutex, not a SQL transaction, across external work.

In enforce mode, `PublishingService.publish_due` acquires this mutex **before
claiming** due articles. Claiming both fresh peers first would make each appear
to be an unresolved prior attempt. After reconciling the current article's own
WordPress identity, the service checks fresh source evidence before preparing
media. `PublicationContext.pre_create_duplicate_guard` then rechecks immediately
before the WordPress creation intent and POST. The mutex remains held through
external-ID persistence and local completion. The legacy batch path uses the
same check-before-media and callback-before-CREATE boundary; its existing outer
job lock is unchanged. A lost named-lock session prevents creation.

At `stage=pre_publish`, a confirmed prior published/post-created/WP-linked
article can reject the current candidate. Merely fresh, pending rich peers
cannot suppress one another. An earlier attempted publication with an unknown
external outcome is not falsely labeled a published duplicate: the candidate
enters `manual_review` with `duplicate_prior_publication_unresolved`, preventing
an endless CREATE retry while preserving the earlier attempt for reconciliation.
The current article's own existing WordPress identity is reconciled normally.

`PublicationRepository.reject_duplicate` atomically verifies the current claim,
source identity and canonical external proof, then sets the unpublished rich
article to `publish_status='duplicate'`. Its raw selection is consumed
(`processed=1`, `chosen_for_publish=0`, schedule cleared). No canonical WP ID is
copied into the rejected row; it remains in the assessment audit. Published or
already WP-linked current rows are never converted to duplicates. A live raw
processing owner retains its claim until it can finish safely. Durable and
legacy due queries do not reacquire terminal duplicates. Successful duplicate
disposition increments `duplicates_rejected`, not publication failures.

Lock acquisition failure defers before any durable claim or media work.
Application-database/audit unavailability after a claim also defers creation.
A genuinely fresh owned first attempt can be reset without consuming its
first-CREATE permission; older ambiguous attempts require manual review.
`duplicate_guard_deferred` records these outcomes separately from failures.
Errors from the publisher body retain their ordinary failure behavior rather
than being mislabeled lock failures. Shadow/off modes do no duplicate-lock I/O.

The guard coordinates publishers running this code against the same application
database. It cannot serialize unrelated manual WordPress creation or an older
worker that does not participate in the lock. Deploy participating workers
together before activating enforce mode. It does not query WordPress for other
events; it uses the locally persisted external identities and existing own-post
reconciliation.

## Verification

The regression fixtures cover all six reported site patterns, material updates,
related events, broad overlap, distance-only matches, missing embeddings,
concurrent workers, final pre-CREATE rechecks, claim ownership and lock loss.
Offline discovery explicitly disables opt-in integration suites and blocks real
HTTP/database/socket connections. The opt-in
`tests/integration/test_duplicate_enforcement_mariadb.py` adds actual SQL
disposition and concurrent named-lock tests with synthetic CMS effects. It
accepts only loopback and the dedicated `coincourier_duplicate_test` schema.
Its destructive fixture reset is limited to that disposable test schema.

Follow-up validation on 2026-10-28: **605 targeted tests passed**, no skips, failures,
errors or blocked-network attempts. The exact module counts are:

| `tests.` module | Tests |
| --- | ---: |
| `test_duplicate_detection` | 30 |
| `test_duplicate_enforcement` | 38 |
| `test_duplicate_cross_source` | 21 |
| `test_duplicate_processing_enforcement` | 20 |
| `test_duplicate_semantic_enforcement` | 7 |
| `test_duplicate_publication_guard` | 20 |
| `test_duplicate_publication_repository` | 19 |
| `test_durable_state` | 62 |
| `test_processing_retry_invocation` | 7 |
| `test_publishing` | 48 |
| `test_publication_hotfix` | 1 |
| `test_publication_identity` | 17 |
| `test_publication_identity_idempotency` | 23 |
| `test_semantic_shadow` | 29 |
| `test_semantic_retrieval` | 26 |
| `test_semantic_calibration` | 25 |
| `test_vector_store` | 9 |
| `test_embeddings` | 40 |
| `test_embedding_operations` | 36 |
| `test_embedding_fresh_start_cutoff` | 12 |
| `test_embedding_fresh_start_config` | 9 |
| `test_deployment` | 47 |
| `test_openai_enrichment` | 14 |
| `test_yoast_metadata_sync` | 45 |

Full offline discovery: **1,198 run, 1,136 passed, 62 skipped**, zero failures or
errors and no blocked-network attempts. The skips were the explicitly opt-in
MariaDB suites: vector/embedding 26, durable state 24, migration tooling 7,
job locking 2 and duplicate enforcement/shadow 3.

All **62 integration tests passed separately**, with zero skips, against the
disposable local `11.8.9-MariaDB-ubu2404` server. A socket guard allowed only its
loopback port. The owned tmpfs-backed container was stopped and removed after
verification; no production database or persistent volume was used.

The negative controls remain publishable: changed FOMC range/vote, subsequent
guidance or market moves, different reporting dates/actions, distinct named
funds, different protocol milestones and policy-rule targets, unresolved date
roles, and insufficient structured evidence despite a close vector match.
Tests also prove raw and pre-publish shadow/enforce call the same engine, shadow
continues through paid work and CREATE, enforce rejects before media/CREATE,
and concurrent cross-source candidates yield one publication. Existing retry,
restart, enrichment and metadata tests retain their original behavior checks;
their expected summary dictionaries now include additive zero-valued shadow
counters.

## Follow-up changed files

Under `GetNewsAPI/`, production changes are limited to
`duplicate_detection/{enforcement,event_matching,policy,publication}.py`,
`gpt_processor.py`, `publishing/service.py` and
`publishing/wordpress/publisher.py`. Test changes are
`tests/test_duplicate_cross_source.py`, `tests/test_duplicate_enforcement.py`,
`tests/test_duplicate_processing_enforcement.py`,
`tests/test_duplicate_publication_guard.py`,
`tests/test_duplicate_semantic_enforcement.py`, `tests/test_durable_state.py`,
`tests/test_openai_enrichment.py`, `tests/test_processing_retry_invocation.py`,
`tests/test_publication_hotfix.py`, `tests/test_publishing.py`,
`tests/test_yoast_metadata_sync.py`, and
`tests/integration/test_duplicate_enforcement_mariadb.py`. This document is the
remaining changed file (20 files total). No migration or configuration file is
changed by the follow-up.

## Schema, activation and rollback

No new migration is required. Existing durable-state migration
`maintenance/migrations/002_phase2_durable_state.sql` uses unrestricted
`VARCHAR(16)` status columns; `duplicate` fits. Existing migration
`007_phase5_duplicate_shadow.sql` supplies the JSON audit storage. Enforce mode
requires those existing tables/columns to be present. No migration or runtime
environment change is applied by this work.

Code deployment alone retains the default `shadow` behavior. A later, separately
approved activation must explicitly set `DUPLICATE_ENFORCEMENT_MODE=enforce`
after verifying the existing schema. Set it back to `shadow` for policy rollback.
Rollback does not reopen already consumed duplicate rows. Do not repurpose
`SEMANTIC_SHADOW_ENABLED=false` as an enforcement switch.

No historical WordPress posts or published database rows are deleted or changed.
The optional historical duplicate report is not implemented. Image providers,
rescue, attribution, captions, prompts, models, embedding dimensions, scheduler,
targets, topic quotas and SEO configuration are outside this change.
