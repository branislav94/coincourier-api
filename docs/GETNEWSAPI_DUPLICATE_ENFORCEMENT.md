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
deterministic duplicate hook. `shadow` preserves the old observational flow,
including `DUPLICATE_SHADOW_ENABLED`. `enforce` applies the narrow policy
independently of that old shadow flag. Semantic flags retain their own meaning.

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

Title similarity is audit evidence, never sufficient blocking evidence.
Placeholder event IDs are ignored. The upstream event ID is shared across
reporting outlets. Inferred same-event pairs without the trusted ID remain
observational unless identical source content independently proves exactness.

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
semantic distance. Logs contain structured evidence, not article bodies or
exception details. Application-database/audit failure fails open during raw
processing; publication must recheck before any new external post.

Per-article counters include `exact_blocks`, `same_event_blocks`,
`material_updates_allowed`, `related_events_allowed`,
`broad_topic_overlaps_allowed`, `semantic_candidates_evaluated`,
`semantic_candidates_promoted` and `fail_open_unavailable_embeddings`.
The last counts actual unavailable retrieval, not disabled retrieval or an empty
successful search. Pair classifications count assessed pairs; block counters
count the final article decision.

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
