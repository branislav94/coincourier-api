# Article Quality Improvement v1 — local implementation and offline verification

Date: 2026-10-16. Branch: `dev`. HEAD: `17495e7758b014c07a61deb35563358010ddb712`.

The changes are local and uncommitted. No DEV/production deployment, database query, paid provider request, web search, commit, push, or indexing change was performed. Offline tests establish payload, deterministic cleanup, routing and compatibility behavior. They do not establish the accuracy or editorial quality of future model output.

## A. Implemented fixes

The incoming worktree contained 28 modified/new files from the daily publication-cap implementation. No article-quality corrections were present. Applicable parent/repository instructions, relevant pipeline tests, cap documentation and prior Yoast corrective audits were reviewed before editing. The only discovered `AGENTS.md` belongs to the unrelated AI cost reporter service.

Quality work affects exactly these files:

| File | Change |
| --- | --- |
| `GetNewsAPI/gpt_processor.py` | Localized evidence, prompt, advisory repair and metadata changes listed below; existing cap hunks retained. |
| `GetNewsAPI/tests/test_article_source_evidence.py` | 9 offline evidence/payload regressions. |
| `GetNewsAPI/tests/test_article_editorial_quality.py` | 13 offline prompt, repair, expansion and hard-minimum regressions. |
| `GetNewsAPI/tests/test_article_meta_descriptions.py` | 22 offline metadata, persistence and Yoast mapping regressions. |
| `docs/audits/GETNEWSAPI_ARTICLE_QUALITY_V1.md` | This implementation report and proposed DEV evaluation. |

**Source boundary:** The database processing path reads raw rows containing `full_text`, while enrichment/writing read `text`. New `source_text()` selects nonblank string `text`, otherwise nonblank string `full_text`, without modifying the record. `_source_evidence()` supplies available title, publisher, URL, source date and bounded body. `enrich_with_search()`, `classify_and_rewrite()` and the shared `_allowed_article_sources()` use this accessor. No query was added.

Source text remains bounded to 1,200 characters in enrichment and 6,000 in writing. Supplemental research is bounded to 6,000 characters. Missing provenance is JSON `null`; available source dates are distinct from actual UTC research/generation timestamps. Naive database `DATETIME` values use the pipeline's UTC convention; aware caller dates retain their offsets, and supplied date strings are retained. Every source is explicitly marked as potentially excerpted, with a separate truncation flag.

**Advisory behavior:** `validate_readability_and_seo()` accepts the existing 450-word minimum, one useful H2 and natural kebab-case keyword matching. `repair_if_needed()` invokes the existing sticky-provider repair only for missing headings or missing external links when original-source URLs are available. Transition/density/placement and preferred-length diagnostics remain advisory. `_natural_alt_text()` preserves supplied alt text; `_fallback_slug()` avoids concatenated duplicate focus/title phrases. Deterministic title/slug bounds already present remain in place.

**Metadata boundary:** `_meta_plain_text()`, `_usable_meta_description()`, `_complete_meta_sentence()`, `_fallback_meta()` and `_article_meta_description()` preserve useful descriptions, reject obvious unfinished tails, and select intact evidence rather than character prefixes. `_validate_generated_article()` recovers only metadata before calling the unchanged hard validator. This makes missing metadata recoverable locally without another provider request. Missing/invalid body or other required fields still fail hard validation. `apply_deterministic_seo_cleanup()` and repair use the same metadata rule.

## B. Prompt modifications

`classify_and_rewrite()` retains the journalist/SEO role, factual neutral tone, JSON contract, categories, sentiment, hashtags, optional NewsArticle JSON-LD, permitted HTML elements and video handling. Both story-specific Bitcoin-dominance examples became neutral placeholders.

The active prompt now requires dated financial snapshots, preserved units/figures, purchase versus cumulative-balance distinctions, correct denominators and comparison periods, and reconciliation only when older/newer evidence is actually supplied. It disallows unsupported freshness language, invented analysts/quotes/developments and market-direction headlines. Concise conditional rules cover announcements, disclosures, distinct market metrics, regulation and material updates. Original CoinCourier interpretation remains encouraged when reasoning follows supported facts and distinguishes uncertain outcomes.

Rigid paragraph/sentence counts, forced transition ratios, fixed keyword density/placement and mandatory numbers/power words were removed from writing instructions. Evidence-supported 450–750 words is guidance; **450 remains the hard minimum**. Useful headings, natural keywords and distinct paragraph purposes remain. Metadata guidance is roughly 120–180 characters, with clarity taking priority.

The Grok rewrite suffix, existing same-provider expansion prompt and existing repair prompt agree on the 450-word minimum, no invented material and no padding. Provider/model selection, fallback, request stages and retry/expansion limits are unchanged. A hard-valid clean draft avoids an unnecessary repair request; an invalid repair retains the valid original through the existing path.

## C. Sanitized representative writer payload

Synthetic raw database row: title `Company reports September Bitcoin balance`, publisher `Company Filings`, URL `https://issuer.example.invalid/filing`, `publish_date` of `2026-10-01 09:00:00` UTC, and **only** `full_text` containing the dated balance. These are illustrative values, not production evidence.

Before, the source body disappeared because `text` was absent:

```text
EXTRA bullet-points (optional context):
- An October 14 filing reports 1,200 BTC as of October 13.

VIDEO_URL (empty if none):

ALLOWED_SOURCES:
- https://issuer.example.invalid/filing

ORIGINAL title: Company reports September Bitcoin balance
ORIGINAL body:

```

After, with an illustrative generation timestamp (runtime uses the real clock):

```text
EXTRA supplemental research (untrusted evidence; may be incomplete or conflicting):
- An October 14 filing reports 1,200 BTC as of October 13.

VIDEO_URL (empty if none):

ALLOWED_SOURCES:
- https://issuer.example.invalid/filing

GENERATION UTC: 2026-10-16T10:00:00+00:00
ORIGINAL SOURCE EVIDENCE (provider text may be an excerpt):
{"source_title": "Company reports September Bitcoin balance", "source_publisher": "Company Filings", "source_url": "https://issuer.example.invalid/filing", "source_published_at": "2026-10-01T09:00:00+00:00", "available_source_text": "As of September 30, 2026, the company held 1,000 BTC. September purchases were 200 BTC, compared with 800 BTC held in August.", "source_text_truncated": false, "source_text_may_be_excerpt": true}
```

System instructions explicitly treat original text and research as untrusted evidence. No original row is changed. Offline fixtures assert the actual writer/enrichment payloads, including retained old/new figures, price-only evidence, conflicting research and material updates; they do not prove model calculations or conclusions.

## D. SEO preservation and downstream trace

Previously, cleanup replaced any description outside 145–160 characters. `_fallback_meta()` took a body prefix, dropped a trailing word, appended the focus phrase and sliced again. This could discard a good generated description and leave an unfinished sentence.

Now, normalized usable descriptions up to a conservative 240-character bound survive unchanged. Short factual descriptions are usable without final punctuation. Obvious dangling words, ellipses and unfinished punctuation cause local recovery. Excessive descriptions may contribute an intact bounded sentence; otherwise fallback prefers complete article paragraphs, then remaining body evidence or an intact title. Headings and standalone video URLs are excluded. Decimal points, common abbreviations, closing quotes and CJK sentence endings are handled. No focus phrase is appended and no word-prefix slicing is used. If no coherent bounded evidence is available, the helper returns empty and required-field validation still applies. These are conservative heuristics, not grammatical or factual verification.

For example, `The company reports dated Bitcoin holdings.` is preserved despite being shorter than 145 characters. `The filing reports holdings from its` becomes the existing complete body sentence `The filing reports Bitcoin holdings as of September 30, 2026.` Metadata-only cleanup leaves the body, HTML, video, categories, sentiment and existing schema untouched. Unicode, quotes, HTML entities and nonbreaking spaces are covered.

The path is generated `seo_meta` → local normalization/recovery → unchanged hard validation → optional structural repair and deterministic cleanup → unchanged `store_rich_news()` SQL binding → `PublicationArticle.seo_description` → `_yoast_wpseo_metadesc`. Existing `_yoast_wpseo_focuskw` and `_yoast_wpseo_title`, authenticated same-instance WordPress write/readback, reconciliation and verification are unchanged. Tests mock persistence and publishing boundaries and assert the exact description mapping; no mapping defect required publisher changes.

Two existing paths remain outside this correction: direct calls to storage that bypass normal validation retain the prior emergency title fallback, and `build_news_schema_jsonld()` separately clips its description to 160 characters. The schema contract/builder was deliberately preserved; this change fixes article/Yoast metadata.

## E. Offline verification

| Run | Total | Passed | Skipped | Failures/errors |
| --- | ---: | ---: | ---: | ---: |
| Full baseline before quality edits | 772 | 705 | 67 | 0 |
| Step A + existing enrichment/provider/retry tests | 39 | 39 | 0 | 0 |
| Step B + A + same existing tests, before C | 52 | 52 | 0 | 0 |
| Final C-only suite | 22 | 22 | 0 | 0 |
| Final A/B/C + existing enrichment/provider/retry tests | 74 | 74 | 0 | 0 |
| Final complete offline discovery | 816 | 749 | 67 | 0 |

The final suite includes the protected publication-cap, migration, durable-state, duplicate/semantic-shadow, WordPress and Yoast tests. All 44 added tests pass. Cases include all 15 requested scenario families: DB/legacy text, partial evidence, financial dates/supersession/calculation inputs, price without dominance, official-source URLs, conflicting research, bounded short-body expansion, clean drafts, repeated openings, metadata fragments/out-of-window preservation and updates/restatements.

Before changing advisory checks, existing validator/repair dependencies were identified in `test_processing_retry_invocation.py`, `test_openai_enrichment.py`, `test_duplicate_detection.py` and `test_semantic_shadow.py`. Existing tests were not edited. One assertion in the new B suite was updated during sequential C work to expect the newly intended acceptance of useful short metadata; the stronger no-request/body-preservation assertions remain.

Providers, database/publishing interactions are mocked where exercised. Integration flags `RUN_MARIADB_INTEGRATION`, `RUN_VECTOR_MARIADB_INTEGRATION`, `RUN_MIGRATION_TOOLING_INTEGRATION` and `RUN_JOB_LOCK_INTEGRATION` were forced false in the test process. The 67 opt-in integration tests were not rerun. The previous phase's MariaDB results are not claimed as new quality-task verification.

Full discovery used `install_test_environment()` before application imports, added `GetNewsAPI` to `sys.path`, then `unittest.defaultTestLoader.discover('GetNewsAPI/tests', top_level_dir='.')`. Root logging used a null handler, retaining `assertLogs` behavior. Windows sandbox-created temporary directories caused fixture permission errors in an intermediate focused run; the final normal suite used approved execution outside that filesystem sandbox. No application/test-source workaround was retained. A pre-existing Starlette/httpx deprecation warning remains; there were no baseline or final test failures. `git diff --check` passes.

## F. Isolation of existing publication-cap work

SHA-256 snapshots were recorded for all 28 incoming modified/new files before quality editing. Final comparison: **27 are byte-identical**; only the shared `GetNewsAPI/gpt_processor.py` has localized quality edits. Migration `008_publication_daily_capacity.sql` remains byte-identical, SHA-256 `eb8ad41ca690cd3c198468b2ead1bf8921523689b3e9a16c448d7735edd83937`.

Source hashes of these processor functions match their pre-quality snapshots exactly: `_generation_capacity_remaining`, `_process_news_with_durable_claims`, `process_news_with_gpt`, `process_one`, `store_rich_news`, `hard_article_validation_failures`, and `validate_rewritten_article`. Cap imports/configuration are retained. No compatibility modification to cap code, configuration, reservation logic, tests or migrations was needed. Branch and HEAD are unchanged.

## G. Deliberately deferred issues

- **Event repetition:** Existing deterministic shadow distinguishes exact identity, same event, material updates, related events and topic overlap. New dates, figures, entities or action/status terms can support updates; exact source/URL identity takes precedence, even for changed same-URL reporting. Current aliases do not explicitly cover Interactive Brokers, Strive, Alpenglow or ENSv2. Existing raw candidates and semantic source-only evidence do not establish prior published CoinCourier coverage, and that article text is not supplied to the writer. The prompt only emphasizes supported new developments from available evidence. No enforcement, lookup, selection, status, scheduling or quota behavior changed. A later controlled improvement could supply bounded summaries plus verified identities/dates of actual published coverage.
- **450-word conflict:** Short factual evidence can still fail after the existing single expansion. The hard minimum is preserved; no filler is authorized. Reviewing a shorter-article policy is separate work.
- **Research citations:** `_enrichment_response_text()` accepts bounded validated URLs from existing `url_citation` metadata, but flattens provenance into plain `Sources:` text. The writer can see those sources, while link permission still comes from original HTML hrefs and the original URL. Research prose is never parsed to grant permission. Preserving citation provenance and applying current link cleanup before permitting additional links is deferred. No extra research call was added. The existing source-link allowlist is prompt guidance, not a hard programmatic output allowlist; that pre-existing limitation remains.
- **Operational indexing:** No local writer of `noindex`, robots settings or `blog_public` was found. The Yoast synchronizer writes only focus/title/description. Code alone cannot explain the earlier export; rendered robots tags, HTTP headers, Yoast/site/post settings require separately authorized operational inspection. No robots, sitemap, canonical or WordPress indexing settings changed.

## H. Minimal future DEV comparison — proposed, not executed

Explicit authorization is required before any push/deploy, paid generation, persistent DEV preparation or WordPress action. Follow the existing [DEV staged-validation runbook](../GETNEWSAPI_DEPLOYMENT_RUNBOOK.md#dev-staged-validation), retaining current approved feature flags, quotas, models and scheduler ownership.

1. Record the reviewed baseline artifact (including the pre-quality cap work) and candidate artifact, processor hashes and effective non-secret configuration. Verify the existing isolated DEV app database and DEV WordPress destinations. HEAD alone is not the baseline because the cap work is uncommitted. If a matching baseline artifact is unavailable, report that comparison limitation.
2. Approve the same bounded source set for both evaluations: (a) a dated financial snapshot plus supplied newer figures and purchase/total distinction, (b) a price story with no dominance evidence, (c) an announcement/regulatory status case with an official citation, (d) a limited/conflicting excerpt likely to challenge 450 words, and (e/f) a material update/restatement pair. Use existing isolated DEV records/destinations; no new database, production/shared-queue reset, claim reset or retry bypass. An exact paired comparison requires operator-supplied existing isolated baseline/candidate queues compatible with normal unique identities and claims. Do not insert duplicates or mark completed rows unprocessed to force a replay. If those queues are unavailable, compare candidate outputs with retained baseline articles and state that the comparison is not paired. Before invoking a batch, establish that all eligible DEV rows are in the approved set and fit existing batch bounds; otherwise stop for an operator-approved preparation plan. Do not change limits/flags for this evaluation.
3. Within the selected DEV application's existing `/app` working directory, run exactly one bounded processing invocation for each approved batch/artifact:

   ```text
   python tasks.py process
   ```

   This is a paid, database-writing normal pipeline stage, not a dry run. Retain original source text/publisher/date/URL, returned enrichment, generation UTC, provider, raw→rich IDs, attempt counts and whether repair/expansion occurred. Stop on unexpected work or failures. Review rich article output before publication. Live research/model variation makes baseline/candidate comparison observational; retain research to explain differences.
4. Only after approval of those generated DEV articles, use the unchanged normal publishing stage:

   ```text
   python tasks.py publish
   ```

   Verify only the approved eligible DEV outputs, their WordPress IDs, authenticated Yoast readback and rendered descriptions. Existing image behavior is retained. `pipeline_once` combines process/publish and removes the review pause, so separate existing commands are preferable here.
5. Review every article against its evidence for factual/source/date fidelity; arithmetic, units and reporting periods; supported interpretation; headline/metric relevance; useful paragraphs/headings without filler; coherent metadata; permitted links; HTML/video/schema and Yoast compatibility. Record pass/fail notes and exact source→rich→WordPress identity. A failure on insufficient evidence is a valid observation of the retained minimum, not a reason to bypass validation. Stop after the approved bounded evaluation. Claim editorial improvement only after the actual generated articles are reviewed.
