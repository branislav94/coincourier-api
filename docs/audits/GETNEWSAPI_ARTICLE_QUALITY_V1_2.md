# Article Quality v1.2 — targeted corrections and offline verification

Date: 2026-10-16. Branch: `dev`. Starting HEAD: `51af05bfee742a342d452890b1a0014f79ea974b` (v1.1). The existing Daily Publication Capacity work was snapshotted before edits. Following the security review, selective staging and independent offline snapshot verification are authorized; commit, push, deployment, paid requests and external data changes remain prohibited.

## A. Confirmed defects, observations and corrections

| Finding | Evidence and confidence | v1.2 result |
| --- | --- | --- |
| Original publisher tracking URL bypasses normalization | Confirmed implementation defect: `_allowed_article_sources()` previously inserted raw `news_url`. Rich 614 retains Benzinga's `utm_source=snapi`. | Normalize original primary/extracted sources and clean matching approved hrefs before storage. Raw identity is unchanged. |
| Research annotation URLs do not reach writer link permissions | Confirmed code path: annotations become a plain Sources suffix; the allowlist previously used original evidence only. | Carry up to three annotation records in a private article scope; writer and repair share the same list. |
| Grok articles sometimes contain zero commas | Confirmed final-output observation for Rich 609, 611 and 614; Rich 613 has commas. No known comma-stripping transform was identified. | Add optional stage observability only. No additional punctuation instruction, heuristic rejection or comma insertion. The exact responsible generation stage remains unknown. |
| Rich 613 omits the newer bStocks announcement | Confirmed omission from available final output. Full research responses were not retained; different AUM populations cannot be substituted automatically. | Strengthen the existing research request's freshness, cutoff and scope instructions. Retrieval improvement is unproven. |
| Rich 612 headline ambiguously attributes the appeal to Chainalysis | Editorial ambiguity supported by the final headline and body. Evidence provider and appellant must be distinguished. | Add a conditional legal-role/headline instruction. No automatic headline rewrite or new validation stage. |
| Rich 614 misstates calendar relationships | Confirmed comparisons: October 5–14 is nine elapsed days; October 5–15 is ten. | Add exact dates, elapsed-period and arithmetic-baseline instructions. No automatic alteration of stored facts. |
| Original diagnostic redaction could retain signed credentials | Confirmed security blocker in the first local v1.2 capture design: SAS `sig`, GCS signing fields and JWT values could survive in captured strings. Staging correctly stopped. | Replace all content serialization/redaction with metadata-only schema version 2. Signed application URLs and citation permissions remain unchanged. |

The existing v1/v1.1 punctuation, distinct-context, anti-padding, concise-focus and description improvements remain. Repeated expansion is still a known limitation of the unchanged 450-word floor.

## B. Exact implementation scope

Only one existing source file changes for v1.2: `GetNewsAPI/gpt_processor.py`.

Existing functions changed:

- `extract_source_urls()`: delegate to the shared conservative URL helper.
- `call_grok_text()`, `call_openai()`: optional request/response hooks before output rejection. Three existing OpenAI body-dump error messages now log only type/status/character counts, preserving their error/retry behavior.
- `generate_json_with_provider()`, `_expand_json_with_same_provider()`: capture pre-parse text, parsed candidates and hard validation failures without changing expansion eligibility.
- `_enrichment_response_text()`: retain accepted annotation provenance after full response acceptance and normalize its bounded Sources suffix.
- `enrich_with_search()`: one localized freshness/scope instruction paragraph.
- `_allowed_article_sources()`: normalized primary/extracted sources plus scoped annotation URLs; original source first.
- `classify_and_rewrite()`: conditional legal roles, exact comparison periods/baselines and preservation of approved signed URLs.
- `repair_if_needed()`: record actual selected repair checks and selected candidate before deterministic cleanup; same triggers and one repair request.
- `process_one()`: isolate citation scope, clean approved hrefs before hard validation/storage, and record final storage candidate.

New private adapters in that file: `_configured_quality_capture()`, `_capture_quality_stage()`, `_clean_final_article_links()`.

New files:

| File | Purpose |
| --- | --- |
| `GetNewsAPI/article_quality_capture.py` | Opt-in metadata-only recorder, authorization, permissions, strict output schema, suppression and budgets. |
| `GetNewsAPI/article_source_urls.py` | Raw URL validation/normalization, real-anchor extraction and approved href cleanup. |
| `GetNewsAPI/research_citations.py` | Immutable bounded annotation metadata and exception/concurrency-safe ContextVar scope. |
| `GetNewsAPI/tests/test_article_quality_capture.py` | 21 metadata-only diagnostic/security tests. |
| `GetNewsAPI/tests/test_article_source_urls.py` | 25 URL boundary tests. |
| `GetNewsAPI/tests/test_research_citations.py` | 11 metadata/scope tests. |
| `GetNewsAPI/tests/test_article_quality_v12.py` | 19 pipeline/SQL/capture integration tests. |
| `GetNewsAPI/tests/test_article_quality_v12_prompts.py` | 5 request-construction tests. |
| `docs/audits/GETNEWSAPI_ARTICLE_QUALITY_V1_2.md` | This report and bounded evaluation procedure. |

No dependency, configuration file, schema, background job, quota, publishing or existing test file was changed by v1.2.

## C. Generation capture and security limits

Capture is observability, not a generation-quality correction. It is disabled unless `ARTICLE_QUALITY_DEV_CAPTURE_ENABLED=true`. The default path does not configure or write a recorder. Existing requests, retries, fallback and generated results do not depend on capture success.

Supported only in a POSIX DEV container where owner-only permissions can be checked. Windows deliberately fails closed; offline tests mock the permission proof to exercise recording logic. Native POSIX deployment permissions still need operator verification. `APP_ENV` is never used as a DEV gate.

Before enabling, an operator must independently verify the effective DEV database host/name, WordPress origin and web-server output mappings, then create an owner-only authorization JSON outside the checkout. Runtime matches those pinned values to effective `config.DB_CONFIG` and `WP_API_URL`; it does not probe destinations or independently establish that an arbitrary host is DEV. An absent, mismatched, expired or unsafe authorization disables capture only.

Example authorization, with placeholders replaced after independent verification:

```json
{
  "authorize_dev_capture": true,
  "expires_at": "<UTC ISO timestamp within the next 24 hours>",
  "database_host": "<verified DEV database host>",
  "database_name": "<verified DEV database name>",
  "wordpress_origin": "https://<verified DEV WordPress host>",
  "private_output_directory": "/private-dev-evidence/quality-v12",
  "output_not_public": true,
  "approved_raw_ids": [101, 102, 103],
  "max_events": 100,
  "max_total_bytes": 2097152,
  "max_event_bytes": 65536
}
```

Configure only that separately approved DEV process:

```text
ARTICLE_QUALITY_DEV_CAPTURE_ENABLED=true
ARTICLE_QUALITY_DEV_CAPTURE_DIRECTORY=/private-dev-evidence/quality-v12
ARTICLE_QUALITY_DEV_CAPTURE_AUTHORIZATION=/private-dev-evidence/authorization.json
```

The authorization file must be owner-only (0600). Its parent and output parent must already be safe; the helper creates only the immediate output directory and a unique run directory, requiring 0700, then creates exclusive 0600 event files. Paths must be absolute, outside the application checkout, free of symlink components and recognized public-directory names; unsafe writable ancestors are rejected except sticky temporary ancestors. The exact output path and its nonpublic status must also be attested. Filesystem checks assume a trusted owner/administrator; they cannot protect against malicious code running as the same OS user.

Bounds are at most ten approved positive integer raw IDs, 200 events and 5 MiB per process run, 64 KiB per event and authorization expiry at most 24 hours away. Defaults are 100 events/2 MiB/64 KiB. Records contain a small fixed metadata schema. A record exceeding an event or total-byte limit is not written. Exhaustion/expiry/write failure stops recording without stopping generation. A cached recorder keeps the budget across stages. Restarting or changing capture configuration starts another run and requires separate operator control; these limits are not billing quotas.

Events cover:

- `rewrite.request/response`, `expansion.request/response`, `repair.request/response` at the existing provider calls, including empty/truncated final responses before rejection.
- `<phase>.pre_parse`, `<phase>.parsed`, `<phase>.validation_failed`.
- `repair.checks`: full soft status and the exact selected trigger list, including an empty list when no repair runs. Later warnings are not mislabeled as repair triggers.
- `repair.selected_before_cleanup`: the retained original or selected repair candidate immediately before deterministic SEO cleanup.
- `repair.post_cleanup`: after deterministic SEO cleanup, on both repair and no-repair paths.
- `article.post_cleanup`: after final approved source-href cleanup and existing schema/video handling.
- `article.before_storage`: after existing schema/video handling, approved href cleanup and final hard validation.

The fresh output schema contains only: schema version 2; authorized numeric raw ID; controlled stage/provider/model-role identifiers; bounded attempt number or null; internally generated UTC and validated expiry; response character count or null; finite finish reason and parse/validation statuses; fixed symbolic validation/repair-trigger codes; allowlisted boolean checks; article word/punctuation counts; and SHA-256 of `full_text` with `article_html` scope. Model roles are `grok_text`, `openai_text` or `unknown`, not arbitrary configured model strings. The corresponding actual model remains identified by the reviewed configuration. Provider request/response events have exact attempts; parsed/final events use null rather than inventing one.

`full_text` is inspected only in memory before the corresponding transformation. Word count matches the existing hard validator. Punctuation counts come from visible article text after tag removal/entity decoding. Raw JSON syntax commas are never article-body comma counts. Invalid JSON, non-object responses and missing bodies have separate parse statuses and null article metrics. Unknown multipart content suppresses subsequent body measurements for that raw ID. Hashes identify equality/differences; they do not disclose text or explain the cause of a difference. HTML-only URL cleanup can change a hash while leaving prose punctuation unchanged.

Never serialized: request messages, raw responses, article HTML, original evidence, research, URLs, auth headers, cookies, API keys/tokens, signed query values, hidden reasoning, exception messages, arbitrary string fields or nested input dictionaries. Requests/unknown fields are ignored without traversal; provider SDK objects and reasoning fields are never passed. Every output string is a fixed symbol, internally generated timestamp, or hash. Unknown identifiers/codes are rejected, mapped to `unknown`, or omitted. Exact primitive types are required; there is no `str()`, `repr()`, generic object dump or fallback serialization. The recorder does not read or retain configured secrets and no longer relies on redaction patterns or sensitive-key names.

The original credential blocker is therefore eliminated structurally. Independent in-memory canary tests include SAS, GCS, JWT, mixed-case, percent-encoded and unknown signing fields in requests, research, HTML, ordinary fields and every string-capable metadata input. Poison objects whose conversion/traversal methods raise confirm that no fallback conversion is used. No sentinel, source URL or content reached output or diagnostic logs. No new database or application state is persisted beyond the authorized metadata files.

Retention is manual: disable capture after the single approved batch, review only in the private directory, then delete the exact owned run directory after review and no later than authorization expiry. Verify the resolved run path is beneath the configured private output root before deletion; never use broad cleanup globs. Delete the expired authorization too. No automatic cleanup job is added, so failure to perform this procedure leaves sensitive artifacts on disk. Do not upload or commit captures.

## D. URL behavior before and after

| Input | Normalized approved destination |
| --- | --- |
| `https://www.benzinga.com/crypto/cryptocurrency/26/10/62351460/is-a-stronger-dollar-bullish-for-bitcoin?utm_source=snapi` | `https://www.benzinga.com/crypto/cryptocurrency/26/10/62351460/is-a-stronger-dollar-bullish-for-bitcoin` |
| `https://issuer.example.invalid/report?id=12&utm_source=feed&page=2` | `https://issuer.example.invalid/report?id=12&page=2` |
| `https://issuer.example.invalid/report?id=12&page=2` | Unchanged. |
| `https://issuer.example.invalid/report?utm_source=feed&sig=a%2Bb#section` | Entire URL unchanged to avoid breaking its signature. |

Recognized trackers remain `utm_*`, `ref`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`. Meaningful raw query bytes, ordering, duplicates, blank values and percent encoding remain. Signed/authenticated URLs retain all bytes, including possible trackers/fragments, rather than guessing signature coverage. Unrecognized signing conventions remain a limitation.

Raw URL normalization is idempotent. HTML entities decode exactly once at an actual quoted href boundary; final replacement re-escapes ampersands/quotes. Nested entities cannot acquire different meanings through repeated normalization. Malformed protocols/hosts/ports, credentials, controls, unsafe characters and Wikipedia hosts do not enter source permissions.

Cleanup replaces only approved href value spans, preserving surrounding HTML, punctuation, names, dates and values. Unapproved links, unrelated internal links, comments/scripts and video elements remain untouched. `news_url`, SQL identity, deduplication and WordPress links/behavior are unchanged; legacy and durable SQL binding tests verify the exact raw identity and cleaned body.

This is approved-source cleanup, not a new hard outbound-link validator. It does not authorize or remove arbitrary model-generated links.

## E. Freshness and factual prompt changes

The same enrichment call now prioritizes newer material developments about the same entity/product/event, primary issuer announcements, filings and official data before the existing Research request UTC cutoff. It preserves dates, definitions, measurement scopes, platforms and geographic populations, surfaces relevant superseding developments and prohibits inferring source absence from a missing search result. Different-scope AUM figures must not be substituted or used to infer growth.

Writer guidance conditionally identifies the actual appellant/defendant/regulator/court, separates evidence providers from litigation parties and requires headline subjects/actions to match the evidence. Comparison guidance preserves exact endpoints, distinguishes elapsed days from labels such as last week, checks the stated arithmetic baseline and avoids incompatible observations/windows.

No research model, provider, reasoning/context setting, token limit, routing, request count or later fact-checking stage changes. Synthetic fixtures include older AUM, newer different/same scopes, before/after cutoff announcements, incompatible definitions, Sterlingov/Chainalysis and October 5/14/15. Passing payload assertions proves instructions/evidence were sent; it does not prove future retrieval, reasoning or editorial accuracy.

## F. Citation provenance implemented

`_enrichment_response_text()` reads only actual accepted `url_citation` annotations from completed search-grounded assistant output. It normalizes/deduplicates up to three safe URLs (at most 1,000 characters each), retains bounded titles and valid optional spans relative to the associated text part, and publishes the immutable metadata only after full response acceptance. Bad optional spans are omitted, never invented. Rejected output clears the scoped metadata.

The public enrichment return stays a plain string. The existing 6,000-character enrichment context bound and three-source suffix limit remain; titles/spans do not add writer context. `process_one()` activates a private ContextVar scope that resets after success or failure, including nested/concurrent processing. No text cache, richer public return type, public argument change or stored source-row mutation is introduced.

Writer and repair both call the same scoped resolver: normalized original primary source first, original evidence hrefs, then annotation URLs. URLs from generated research prose, including a forged Sources section, never grant permission. Original attribution remains available, and extra citations are optional.

Annotation origin proves provider provenance, not factual verification, authoritative status, freshness or claim-to-source support. Metadata is in memory only; full provenance is not persisted. Existing outbound membership remains prompt-enforced. A hard link-enforcement or claim-verification system is deferred.

## G. Offline verification

| Run | Total | Passed | Skipped | Failures/errors |
| --- | ---: | ---: | ---: | ---: |
| Complete pre-change baseline | 826 | 759 | 67 | 0 |
| Original v1.2 mixed-tree discovery, before security correction (historical) | 909 | 842 | 67 | 0 |
| Metadata-only capture tests | 21 | 21 | 0 | 0 |
| Corrected quality/v1/v1.1/enrichment/provider/security focused run | 127 | 127 | 0 | 0 |
| Corrected complete mixed-tree offline discovery | 907 | 840 | 67 | 0 |

The corrected mixed-tree full run took 7.161 seconds. Metadata-only tests replace obsolete raw-evidence/redaction expectations; unrelated tests are unchanged. The 67 external-service integrations stayed disabled, matching baseline. The pre-existing Starlette/httpx deprecation warning remains. Mixed-tree results include unpublished capacity work and do not establish isolated compatibility. Exact staged-tree focused/full verification, absence of capacity modules/migration 008, and snapshot identity are reported separately at staging review; no mixed-tree result should be substituted for those checks.

Tests cover secure opt-in/fail-closed behavior, exact destinations, expiry, approved IDs, limits, strict metadata-only output, canary/poison inputs, unexpected multipart suppression, parsed-only body metrics, no extra provider calls, unchanged results under capture failures, pre-rejection stage measurements, exact selected repair checks, parsing/expansion, annotation lifecycle, normalized permissions, HTML escaping and legacy/durable SQL persistence. Existing cap, durable processing, fallback, SEO/Yoast, publishing/reconciliation, shadow and image tests remain.

All external interactions in exercised tests are mocked or use local SDK mock transports. No live integration was enabled. Windows fixture tests required authorized execution outside the filesystem sandbox; no sandbox rejection or pending approval remains.

## H. Publication-cap and Git preservation

The 28 pre-existing dirty/untracked cap files were hashed before v1.2. All 27 other than shared `gpt_processor.py` remain byte-identical. That shared file has only the localized quality changes listed above.

All 52 other existing function source hashes in `gpt_processor.py` match baseline. In particular, `_generation_capacity_remaining()`, `process_news_with_gpt()` and `_process_news_with_durable_claims()` are unchanged. Routing wrappers, `_should_try_same_provider_expansion()`, hard validation, `store_rich_news()`, `mark_processed()` and deterministic SEO cleanup are also unchanged. The 450-word hard minimum, exact underlength trigger, publication validation, provider selection and repair limits remain.

The index was empty at the start of the security correction; HEAD remains unchanged. Selective staging is authorized only after the corrected security checks pass. A HEAD-to-working-tree diff necessarily includes the user's pre-existing cap work and must not be staged wholesale. The quality-only index must exclude capacity imports/constants/functions, scheduling hunks, all other cap files and migration 008; it is tested as a separate disposable tree without copying workspace files or credentials.

Protected nonshared files and matching SHA-256:

| File | Before = after SHA-256 |
| --- | --- |
| `.env.example` | `2e7dd53becd20a841a38511b1248e5cebc2b31b2e92b4aa0b091e76d01eadfc1` |
| `GetNewsAPI/config.py` | `34fa1c7bfa4ab5a3506fecdea685e34aecda11d7b1d3c3c4a191273c2a6d2399` |
| `GetNewsAPI/deployment/migrations.py` | `c3448ebd18e40784377fb87c1570ccfd6aa5b05ec8c6de316cd8a813f9b759c9` |
| `GetNewsAPI/deployment/service.py` | `52dfbac61f8f11d79f2bfbbc7c3c039f6f93949caa8cd5773ffbd2b9994797ee` |
| `GetNewsAPI/deployment/verification.py` | `0791c4ab86d375a1ac9223bc1db9c8d10c69c14dcdb9ea5d0ae04979f0bd4487` |
| `GetNewsAPI/fetcher.py` | `ce4bca82ca159415a039e61aa115ba082d404b8558ea2e346fbd6d5fa1456fa3` |
| `GetNewsAPI/operations/jobs.py` | `3a930eef78a55bca3540ecc345ea608818de05ea3ae9ab700c9d3ad545846aac` |
| `GetNewsAPI/publishing/models.py` | `e9eb29b66b35de7c7aa3a89f5124994fd6eb4b74458f603dfa020a875519d6d2` |
| `GetNewsAPI/publishing/service.py` | `9f58f775627d30a6ce1fac28a0880c65192d5cf05d6ed52070993961b94adbd1` |
| `GetNewsAPI/publishing/wordpress/publisher.py` | `bd7ec0cf24cf052f20a36b0d640de615ec18ff8a4c347912e4bffb4e37fcecaf` |
| `GetNewsAPI/repositories/publication.py` | `27dda5266961b7235c6530d79ebdc302b07e34c7b068e22bbec73ec5c786a3b5` |
| `GetNewsAPI/repositories/raw_news.py` | `1dedff3e897efc5e6698e079a86b98cd4dddb018cc3f414c8b6c81d70cc715dd` |
| `GetNewsAPI/runtime/config_validation.py` | `b88b4d698936dfaa59b2bf5553e25295639723d0447884119d2ea90614e59c3f` |
| `GetNewsAPI/tests/environment_isolation.py` | `4098943a77fdddc1aabbfd1590fd560e8bf1d28ea9e272ec764d2a28adf68b55` |
| `GetNewsAPI/tests/integration/test_migration_tooling_mariadb.py` | `fb85e7e63a4896f7406b963339144177b58c2c771e29705ca1d7c9dd6399c2b3` |
| `GetNewsAPI/tests/test_migrations.py` | `249e044ea7bc38286a4a35bd2f62d5d3ee68282fdb1e2489452c1b58c3ee7297` |
| `maintenance/migrations/README.md` | `03943e684616f54b051f83dad00ea54550c07d6a499756342f7eafcc40d1e534` |
| `GetNewsAPI/publishing/capacity.py` | `a35af4018f60e309e2ad45b5af7b4bd85190a3a9de3a985063c1ef7148fca22b` |
| `GetNewsAPI/repositories/publication_capacity.py` | `599a16efd984ef872347d877566931d49a87120a02f4ecb2e7a5c48a8ca578b8` |
| `GetNewsAPI/tests/integration/test_publication_capacity_mariadb.py` | `9a228ef9b238c2436338c2d9b73317c900c818ed18242f46ea812c34f34367d8` |
| `GetNewsAPI/tests/test_publication_capacity.py` | `47181b7cb2b041cadc516a3af24293e1fd60eac6c12f84019c93c9eb443711a6` |
| `GetNewsAPI/tests/test_publication_capacity_admission.py` | `cc17dc66c01e559beea32c0613f747c5b7ec9bf78f2bea4fe810c20c4f126922` |
| `GetNewsAPI/tests/test_publication_capacity_config.py` | `ae6e5c2dbb9804abb8372ede261b517472503e666a28f4701d64af8bc1c3a07d` |
| `GetNewsAPI/tests/test_publication_capacity_jobs.py` | `35bc01f079615ed9ef42470b59dc7d41ac79731d742f2fd443f178abd4da81b3` |
| `GetNewsAPI/tests/test_publication_capacity_publishing.py` | `f6cb6aca6c9631ba81d6e7b205390904047ca370dd91e72635331889e5d480f2` |
| `docs/GETNEWSAPI_DAILY_PUBLICATION_CAP.md` | `c6b4c997f2544a448daeaa522260798d47f8e11a85bb4a923a4f9b6fcdf2f4ab` |
| `maintenance/migrations/008_publication_daily_capacity.sql` | `eb8ad41ca690cd3c198468b2ead1bf8921523689b3e9a16c448d7735edd83937` |

## I. Remaining limitations and deferred work

- The punctuation-loss stage has not been identified or fixed; there are no captured historical intermediate responses. Capture improves the next diagnosis only.
- Prompt changes do not guarantee freshness, factual headlines, correct arithmetic, concise phrasing or less repetition. Human DEV review is required.
- The unchanged 450-word floor can still trigger unsupported-feeling expansion and fallback. A separate approved brief-news policy evaluation remains appropriate; no 300–450-word eligibility or threshold change was implemented.
- Citation metadata does not verify truth or support, and final outbound allowlist enforcement is not new hard validation.
- Signed URLs intentionally may retain trackers; unknown signing formats and ambiguous malformed/unquoted HTML are handled conservatively rather than broadly rewritten.
- Capture requires independently verified DEV destinations, POSIX permission proof and manual retention cleanup. On this Windows workstation, actual capture remains disabled. Suppression, failed parsing or exhausted budgets can leave incomplete measurements. Raw contents are intentionally unavailable; counts/hashes can locate a stage difference but cannot establish its editorial cause.
- Existing application exception diagnostics remain outside a general logging redesign; this phase removes the three explicit OpenAI response-body dump statements and introduces no new body logs.

## J. Next bounded DEV evaluation — proposed, not executed

After security and isolated staged-tree review, obtain separate commit/deployment authorization. Before any DEV run, independently verify the DEV database and WordPress destinations, prevent publication through the existing controlled DEV procedure, choose at most three raw IDs, a short expiry and one private capture run. Do not deploy the mixed cap working tree.

Approved IDs filter evidence only; capture does not limit paid processing or disable publication. Use the existing controlled DEV invocation to process only those selected rows and stop after the batch. Do not rely on the capture flag or its budgets for those controls.

Use one small batch covering: a bStocks/AUM freshness and scope case, a legal appeal/evidence-provider case, and a dated market-comparison/Benzinga URL case. Keep all outputs unpublished. Use the existing routing and exactly the existing research/generation calls; do not rerun merely because punctuation counts look unusual.

For each article, compare the metadata at request → initial assistant response → parsed draft → expansion (if actually triggered) → selected repair checks/response (if triggered) → selected candidate → post-cleanup → pre-storage. Compare word/comma counts and hashes at the first divergence. Request events have no body statistics. A parse failure has no article punctuation count. Counts combine numerical/date and prose punctuation; separate those only through a separately authorized human content review, not by persisting content in diagnostics. Record missing/suppressed stages as inconclusive.

Review actual source fidelity, exact legal actor/headline, compatible measurement scopes and as-of dates, cutoff eligibility, elapsed-day arithmetic/baselines, concise SEO focus, repeated caveats, annotation provenance and final source hrefs. Check that expansions/fallbacks reflect the unchanged rules. No observation from this small sample alone establishes production quality.

Disable capture after the batch, retain only private review evidence until expiry, then delete the verified owned run directory and authorization. Decide on rollout only after reviewing that evidence. Any remaining punctuation or length-policy correction should be separately scoped and approved.
