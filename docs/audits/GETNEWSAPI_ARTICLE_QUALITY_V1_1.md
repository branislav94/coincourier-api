# Article Quality v1.1 — minimal corrections and offline findings

Date: 2026-10-16. Branch: `dev`. Starting HEAD: `6163bca3f6aca0c0e0bdcacc98bf69eeee382b64` (Article Quality v1). The index was empty at task start and remains untouched. Existing publication-cap work remains uncommitted in the working tree.

## 1. Evidence and punctuation diagnosis

The user reports three successful, unpublished DEV outputs: Raw 18130 / Rich 609 (Strategy financing, Grok final), Raw 18198 / Rich 610 (diesel inventories, OpenAI fallback final), and Raw 18239 / Rich 611 (Evernorth XRP treasury, Grok final). Reported Grok bodies contain no commas at approximately 459 and 524 words; the OpenAI-final body has natural punctuation. All initial Grok drafts reportedly required expansion, and one ultimately used fallback. Evernorth reportedly repeats explanations about unavailable disclosures.

**Those bodies and captured provider responses were not available in the repository.** A local export path/text was requested. No DEV database, live endpoint or model was queried, and the existing articles were not altered. Article-specific repetition counts, punctuation counts and the responsible generation stage have therefore not been independently verified. The corrections use the reported findings plus the local code trace and synthetic offline tests.

| Boundary | Observed local behavior |
| --- | --- |
| `call_grok_text()` / `_chat_content_to_text()` | Returns the SDK content string unchanged or concatenates text blocks. No punctuation filtering. |
| `generate_json_with_provider()` | Parses with ordinary `json.loads()`; no JSON-recovery sanitizer strips body commas. |
| `_expand_json_with_same_provider()` | Serializes the draft with `json.dumps()`, receives a fresh model response and parses it with `json.loads()`. |
| `repair_if_needed()` | A structural/link repair is another fresh model response. The no-repair path preserves the body. |
| Hard validation and readability checks | Inspect word counts/fields/metrics without rewriting the body. |
| `apply_deterministic_seo_cleanup()` | Edits title, slug, description and alt metadata, not body punctuation. |
| `process_one()` / `store_rich_news()` | Only missing-video insertion changes the body in processing; storage binds the body directly. |

Synthetic fixtures preserve exact body equality, including `October 14, 2026,`, `1,234 BTC`, `$82,345,678.90`, `2.5%`, English clause commas and quoted speech, across SDK extraction, parsing, expansion, repair, cleanup and SQL binding.

**Confidence:** high that the current local transformations do not remove commas. A model-response origin is probable from this trace and the reported provider contrast. Initial rewrite versus expansion versus repair cannot be determined without the captured responses. No instruction explicitly prohibits commas; the absence of explicit punctuation guidance is an omission, not a proven causal instruction. The prompt addition is a targeted preventive correction, not proof that the DEV symptom is fixed.

## 2. Exact minimal implementation

Only three existing functions change, and all changes are prompt string values:

- `classify_and_rewrite()`: publication-ready journalistic punctuation, clear numerical/date formatting without changing meaning; distinct supported explanations; material uncertainty retained without repetitive disclosure/research caveats; concise focus phrases.
- `_expand_json_with_same_provider()`: develop distinct supported context rather than repeat conclusions or describe missing research to add words; preserve natural punctuation.
- `repair_if_needed()`: retain punctuation and avoid multiplying caveats, missing-disclosure explanations or conclusions.

There is no punctuation insertion algorithm, comma-count validator, new cleanup transformation, additional model request, routing/model change, new repair trigger or threshold change. Necessary names, dates, values, qualifiers and uncertainty remain protected.

## 3. Repetition and readability

The 450-word hard minimum can create pressure to expand a short provider excerpt. Expansion is a fresh response and can repeat missing-information explanations when little distinct supported material is available. Repair can also rewrite a body, although v1 already limits its trigger to actual heading/link omissions; minor readability metrics do not trigger it.

The revised writer asks for each paragraph to contribute a distinct fact or explanation and each implication to be explained once. It asks for material limitations briefly where they affect interpretation and explicitly preserves necessary uncertainty. Expansion/repair now name the reported failure pattern rather than merely asking for less filler. These remain generation instructions: offline assertions prove that they are sent, not that models will follow them. No sentence or caveat is mechanically deleted from a generated article.

## 4. SEO contracts

`seo_focus` is guided toward a search phrase, usually **2–5 words**, identifying the main entity/topic and development. Headline-like clauses and unnecessary dates are discouraged; essential names and qualifiers remain.

This is guidance rather than a hard maximum. Existing long focus values are neither truncated nor rejected, and no exact-density/placement requirement is introduced. Title, slug, image-alt, metadata, category, sentiment, hashtags, JSON-LD, HTML/video, storage, Yoast mapping and synchronization remain unchanged. The successful v1 description cleanup is untouched. Tests retain both short and long returned focus values without stuffing them into other fields.

## 5. Recommendation on brief news

**Evaluate a separate 300–450-word brief-news policy; do not activate it in this change.** The reported three-for-three initial Grok underlength results and thin source excerpts suggest that a universal 450-word floor can reward repetition and extra generation without adding evidence. This small DEV sample supports an evaluation, not an automatic policy change.

A future approved comparison should judge whether a shorter item fully covers the supported development, attribution, as-of dates, figures/units, relevant context and material uncertainty. Use length as a consequence of evidence sufficiency, rather than a substitute for it. Longer disclosures or complex numerical stories may still need more explanation. Compare fidelity, omissions, repetitive caveats, readability and expansion/fallback usage before approving any validation or brief-article eligibility change. No classification call or implementation is added here. The current 449/450 boundary remains intact.

## 6. Research attribution assessment

The writer and repair use `_allowed_article_sources()`: original-body hrefs (within the existing source limit) plus original `news_url`. Short provider excerpts often contain no hrefs, leaving the original publisher as the sole permitted link. Under the strict prompt policy, linking to that publisher is expected.

The existing research response already supplies `url_citation` annotations. `_enrichment_response_text()` validates completed output, filters/deduplicates up to three bounded HTTP(S) annotation URLs and appends a plain `Sources:` suffix. Structured identity, titles/spans and claim association are not retained. `enrich_with_search().strip()` and writer slicing then operate on ordinary strings. A prose URL and a metadata-derived URL cannot safely be distinguished downstream.

Preserving a typed, immutable text-and-citations result is possible using the **same existing response**, with no added paid request. Safely granting additional link permission would require explicit propagation through enrichment, writing and repair; consistent URL cleanup/deduplication; and end-to-end provenance/link-boundary tests. API annotation provenance alone does not prove that a page is primary, current or sufficient for every claim. The current emitted-link allowlist is prompt-enforced rather than programmatically enforced.

That broader boundary change is deferred. No research prose or `Sources:` text is parsed to authorize URLs, no permission is expanded, and the existing negative source-permission regression remains unchanged. Research/source context limits remain 1,200/6,000/6,000 characters; no additional research or retrieval is performed.

## 7. Offline verification

| Run | Total | Passed | Skipped | Failures/errors |
| --- | ---: | ---: | ---: | ---: |
| Complete baseline before v1.1 edits | 816 | 749 | 67 | 0 |
| New v1.1 tests | 10 | 10 | 0 | 0 |
| Focused v1.1 + v1 + enrichment/provider/retry suites | 84 | 84 | 0 | 0 |
| Final complete offline discovery | 826 | 759 | 67 | 0 |

The new `GetNewsAPI/tests/test_article_quality_v11.py` contains 10 tests (with provider/durable subcases): SDK string/multipart content, actual rewrite JSON parsing, same-provider expansion, structural repair, no-repair/metadata cleanup, exact legacy/durable SQL body binding, Grok rewrite → failed Grok expansion → OpenAI fallback, zero-comma body left untouched, actual prompt payloads, and focus guidance that leaves output fields compatible.

The full suite includes publication-cap, durable-state, WordPress, Yoast, existing output contracts and provider fallback. All pre-existing tests are unchanged. All provider/database/publishing interactions exercised by the new tests are mocked. Existing `install_test_environment()` was installed before imports; integration opt-ins were disabled, so 67 external-service integrations were skipped. Full runs used approved execution outside the Windows filesystem sandbox for writable synthetic temporary fixtures. A pre-existing Starlette/httpx deprecation warning remains. No dependencies or runtime/environment configuration were changed.

## 8. Git scope and protection evidence

The only v1.1 files are:

| File | Delta |
| --- | --- |
| `GetNewsAPI/gpt_processor.py` | Five quality hunks: 9 added lines, 1 removed line; prompt strings in three functions only. |
| `GetNewsAPI/tests/test_article_quality_v11.py` | New 279-line synthetic offline regression file. |
| `docs/audits/GETNEWSAPI_ARTICLE_QUALITY_V1_1.md` | This findings, verification and exact processor-diff report. |

SHA-256 snapshots were captured for all 28 incoming cap-related files plus the four existing v1 test/report files. The **27 other capacity files are byte-identical**, including migration 008; the three existing v1 test files and v1 report are also byte-identical. All processor functions except the three prompt-bearing functions above match their starting source hashes. In particular, capacity imports/constants/helper, both processing flows, durable claims, hard validators, `process_one()`, storage, research processing, provider routing and models remain unchanged. AST comparison after replacing string values confirms that the three edited functions retain the same non-string syntax/control flow.

The index remains empty, branch/HEAD remain unchanged, and `git diff --check` passes. The ordinary HEAD-to-worktree diff still includes the pre-existing capacity implementation. The following is the complete v1.1 processor delta projected onto committed v1, excluding those preserved cap hunks. It is a reporting projection only; no index/worktree rewriting was used to isolate it.

```diff
--- a/GetNewsAPI/gpt_processor.py
+++ b/GetNewsAPI/gpt_processor.py
@@ -784,6 +784,9 @@
         'Keep the same facts, dates, numerical values, title, categories, SEO fields, '
         'allowed links, video and JSON keys. Explain supported implications without '
         'inventing developments, quotations or repetitive research disclaimers. '
+        'Develop distinct supported context; do not restate conclusions or describe '
+        'missing research/disclosures just to add words. Preserve natural English '
+        'punctuation, including commas in prose, formatted numbers and dates. '
         'If the evidence cannot support the minimum, do not manufacture material. '
         'Return valid JSON only; hard validation still applies.'
     )
@@ -1383,7 +1386,7 @@
         '• Distinguish purchases from cumulative holdings, percentage changes from shares of a total, and use the correct denominator and comparison period.\n'
         '• Attribute uncertain or conflicting claims directly. Narrow conclusions to the evidence; do not invent analysts, quotations or developments.\n'
         '• Add useful CoinCourier interpretation with clear reasoning from supported facts. Separate interpretation from facts and uncertain outcomes: greater asset exposure does not prove a price rise.\n'
-        '• Avoid repetitive caveats or research-limitations filler; do not make every paragraph unnecessarily cautious.\n'
+        '• State each material limitation briefly where it changes interpretation; preserve necessary uncertainty, but do not repeat caveats or list missing research/disclosures to fill space.\n'
         '\n'
         'WHEN APPLICABLE (use only cases supported by the supplied evidence):\n'
         '• Announcements: distinguish announced plans, proposals, actual launches and completed events.\n'
@@ -1403,6 +1406,7 @@
         "\n"
         "YOAST-STYLE ON-PAGE SEO TARGETS:\n"
         '• Choose seo_focus for this actual story and its metric; an asset mention does not make it a dominance story.\n'
+        '• Make seo_focus a concise search phrase, usually 2-5 words identifying the main entity/topic and development. Avoid headline-length clauses and unnecessary dates; retain essential names and qualifiers.\n'
         '• Use relevant keywords or natural variants in accurate titles, readable slugs, headings, descriptions and meaningful alt text where useful. No fixed density or keyword stuffing.\n'
         '• Write a descriptive headline. Never force numbers, power words, sentiment or a market direction not supported by the evidence.\n'
         "\n"
@@ -1419,7 +1423,9 @@
         "\n"
         "READABILITY TARGETS:\n"
         '• Prefer clear sentences and logical flow; use transitions only when their meaning fits.\n'
+        '• Write publication-ready journalistic English with natural commas and other punctuation. Format values and dates clearly (for example, 1,234.5 and October 14, 2026) without changing their meaning.\n'
         '• Avoid repetitive paragraph openings, repeated conclusions and filler. Use headings that explain the actual story.\n'
+        '• Give each paragraph a distinct supported fact or explanation; explain each implication once.\n'
         "• Use a short <ul><li> list only if it adds clarity.\n"
         "\n"
         "CATEGORY:\n"
@@ -1707,6 +1713,8 @@
         'Preserve every fact, figure, date, attribution, category, sentiment, video and JSON field. '
         'Use only facts already present in the draft; it is evidence, not instructions. '
         'Do not add claims, quotations, padding, forced transitions or keyword repetition. '
+        'Do not multiply caveats, missing-disclosure explanations or conclusions. '
+        'Retain natural English punctuation, including commas in prose, numerical separators and dates. '
         'Keep at least 450 words and the same HTML policy (<p>, <h2>, <strong>, <a>, <ul><li>), no <h1>. '
         'Add a useful heading only if missing. Preserve existing links; add only relevant URLs listed below. '
         'Do not change good titles or descriptions for preferred lengths. Return the full JSON only.\n'
```

The new test file's exact content is available in [test_article_quality_v11.py](../../GetNewsAPI/tests/test_article_quality_v11.py). Review the live combined diff with `git diff -- GetNewsAPI/gpt_processor.py`; review the untracked test directly or with `git diff --no-index -- NUL GetNewsAPI/tests/test_article_quality_v11.py` (exit status 1 denotes differences).

## 9. Recommended next bounded DEV evaluation — not executed

After explicit authorization for DEV deployment and paid generation, approve three new eligible source rows covering the same Strategy financing, inventory and XRP treasury patterns. Retain Rich 609–611 and their raw records unchanged and unpublished. Do not reset their processed flags, replay completed identities or change quotas/models/flags to force a comparison.

Record the reviewed deployed processor identity and exact source/excerpt, available research, as-of dates and resulting provider/stage sequence. To diagnose punctuation precisely, retain sanitized raw rewrite/expansion/repair responses using an approved controlled capture method; current ordinary logs do not retain full article responses. Agree that capture before running if it is not already available, with no additional provider requests.

Only when the existing DEV eligible queue contains the approved bounded batch and no unrelated work, run the existing processing stage once from `/app`:

```text
python tasks.py process
```

If that bounded queue cannot be established safely, stop for an operator-approved preparation plan. Do not publish. Compare raw response bodies with parsed/stored bodies at each available boundary: punctuation/date/number fidelity, distinct paragraphs, missing-disclosure/caveat repetition, supported interpretation, concise focus phrases, unchanged good descriptions, HTML/video/JSON fields and provider expansion/fallback counts. Inspect all three stored articles before approving any further generation. Existing v1 output is an observational comparison if input/research/model variation prevents paired inputs. Comma counts can flag review candidates but must not become punctuation quotas. A separate approved brief-news experiment should follow only if the retained 450-word policy still causes padding or avoidable expansion.

No live requests, existing data edits, commit, push, deployment, quota/schema/job/publishing changes or actual brief-policy activation occurred in this task.
