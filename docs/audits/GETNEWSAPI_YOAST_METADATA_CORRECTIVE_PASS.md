# GetNewsAPI Yoast metadata corrective pass

Verdict: **READY FOR INDEPENDENT REVIEW** (local implementation and offline verification only).

Date: 2026-10-12. Same detached worktree:
`C:\Users\Win11\Documents\GitHub\api-test-yoast-confirmed-fix-20261012-192711`.
Fixed base: `e1ac63b14c9670df0b7e53656ba26559af9f5940` (HEAD and existing origin/dev reference). No fetch or external request was made in this pass.

This report supersedes the retry-safety claims in `GETNEWSAPI_YOAST_METADATA_REGRESSION_FIX.md`. The previous report, patch and all earlier test receipts remain intact as historical evidence.

## 1. Changed-row identity defect and narrow correction

The old `PublicationRepository.save_identity()` populated raw ID/key with COALESCE and delegated to `_owned_update()`, which accepted only one affected row. A valid retry with identical existing identity changes zero rows under normal MySQL/MariaDB semantics. That was incorrectly treated as lost ownership; PublishingService stopped before REST recovery. The earlier SQLite restart adapter reported matched rows and masked the problem.

Only `save_identity()` changes its success rule. It performs a guarded UPDATE and an owned readback on one connection/transaction:

- UPDATE retains ID, claim-token, unpublished and claimed/post_created predicates. Raw ID/key must be null or exactly compatible; supplied post ID/URL expectations use null-safe comparison.
- Capture the UPDATE count before SELECT. Counts 0 or 1 are eligible for verification; other counts fail.
- SELECT the same ID/token/unpublished/eligible row with FOR UPDATE.
- Verify exact raw ID/key, ownership/status and supplied pointer expectations in Python, then commit. Missing, ineligible, stale-token or conflicting state rolls back and returns false.
- Readback also verifies changed-row success; rowcount 1 cannot override a conflicting result.
- Timeouts, commit failures and interruption propagate after rollback/resource cleanup.

PublishingService supplies the post ID/URL from its claim snapshot, including explicit expected NULLs. Omitted optional expectations preserve the older positional call signature. `_owned_update()`, client flags, DB configuration and other claim-token semantics are unchanged. No FOUND_ROWS workaround is used.

The new `test_publication_identity_idempotency.py` independently scripts changed-row counts and returned state, then asserts the actual SQL predicates. It covers owned identical count0 success; wrong token/raw/key/post ID/post URL, missing/ineligible/published state and unexpected cardinality failure; count1 normal success; explicit NULL conflicts; rollback/commit/timeout/interruption behavior. These assertions do not obtain ownership correctness solely from fake database logic.

The restart adapter now computes changed-row counts from actual before/after SQLite rows, translates only the narrow SQL syntax, and explicitly records the zero-change identity update. The second fresh Python process asserts exactly one identity no-op and reaches real repository/service/publisher REST reconciliation. It still does not establish real MariaDB locking/concurrency semantics.

## 2. Old marker and available REST contract

The base publisher wrote these WordPress postmeta keys using direct SQL before saving the application pointer:

`_coincourier_publication_key`, `_coincourier_raw_article_id`,
`_coincourier_rich_article_id`, `_coincourier_source_url`.

When the application ID was absent, old reconciliation looked up an exact publication key in WordPress SQL, obtained the post ID, saved it in the application and continued. Thus a marker committed before an application pointer failure previously supplied another recovery path. It was unsafe to retain that lookup against the known stale SQL view.

Local repository search found no marker REST registration, custom marker endpoint or collection-query implementation. Saved collection/item OPTIONS and discovery evidence expose neither the CoinCourier keys nor a supported marker filter/query route. Unknown query parameters, title search and similar slugs cannot provide identity proof. Authenticated private plugin behavior remains unverified; it was not contacted.

**Supported exact REST marker recovery is not established, so this implementation does not attempt it.** REST-marker-found/foreign-marker tests are not applicable to an unsupported endpoint. Application publication-key conflicts are tested; no fabricated REST recovery contract was added.

Evidence with file hashes and JSON pointers:
`audit/corrective/marker_contract_findings.json`.
The earlier public preflight sources remain in the prior metadata-fix worktree.

## 3. Creation permission and fail-closed recovery

Migration 002 gives historical rows pending/zero defaults and fills publication keys without reconstructing WordPress pointers. Old legacy publishing did not record attempt counts. Consequently pending + zero attempts + null pointer, including a null key, is not proof that no remote post exists.

A new rich-row INSERT now explicitly writes `publish_status='fresh'` in both existing persistence modes. The ON DUPLICATE UPDATE never changes that state, attempts or pointers: regenerating/rewriting an existing row cannot promote historical or attempted work to fresh. This is a publication-state change in `store_rich_news()`; generated content, prompts, models, providers and SEO cleanup are untouched. No schema change is introduced; the existing VARCHAR(16) status column holds the new values.

Freshness requires all seven state fields to exist: published=0, status fresh, exact integer attempt count zero, and null post ID, URL, creation timestamp and claim token. Missing fields, historical defaults or inconsistent pointers fail closed.

Recovery order:

1. A trusted application post ID reconciles that exact REST post, regardless of freshness. Auth/site/ID/type/shape/404 failures retain the ID and cannot create another post.
2. Without an ID, supported marker recovery is unavailable.
3. Any unproven no-ID row returns `post_identity_manual_reconciliation_required`, without native CREATE or image work.
4. Only explicit fresh provenance permits initial creation.

Durable claims consume fresh state atomically by becoming claimed and incrementing attempts before remote work. The first preclaim snapshot can authorize creation only when attempt=1 and the claim was not recovered. Retries without a saved pointer cannot regain freshness.

Legacy publishing installs a callback that commits `fresh -> claimed`, attempt count 1 and claim timestamp immediately before native POST. Its guarded UPDATE requires the same fresh/no-pointer/no-owner conditions and exactly one changed row. A callback failure prevents POST. If POST succeeds but pointer persistence fails, the already committed intent survives. The next attempt or process sees no ID plus prior intent, blocks CREATE and requires manual reconciliation.

Manual-required rows become `manual_review` instead of retryable. Durable deferral preserves exact claim-token and eligibility guards and requires no post ID; legacy deferral requires no post ID and no durable claim token. Neither clears post/media identity. Count/fetch selection excludes manual_review, and durable claim eligibility excludes it, so an old ambiguous row cannot consume every batch slot and prevent later fresh/linked work. Ordinary metadata failures with a saved ID remain retryable.

| Available application state | Automatic behavior |
| --- | --- |
| Existing trusted wp_post_id | Exact REST recovery; no CREATE |
| Explicit fresh, zero attempts, all pointer/owner fields null | First CREATE allowed after durable claim/legacy intent |
| Historical pending/null status, including zero attempts | Manual review if no ID |
| Retryable/recovered/claimed/post_created without ID | Manual review; no CREATE |
| URL/creation timestamp without ID, missing state fields or inconsistent fresh state | Fail closed; manual reconciliation or visible schema/state failure |
| manual_review | Excluded from automatic selection; operator reconciliation required |
| Already published | Existing publication queries exclude it |
| Another durable owner in legacy mode | Deferral refuses to clear that claim token |

No historical row is automatically marked fresh, matched by title/slug, repaired or linked. Manual reconciliation must use trusted application/owning-REST evidence and preserve existing publication identity.

## 4. Duplicate-safety evidence and preserved REST behavior

Tests prove:

- A metadata timeout after committed identity survives a real two-process restart; the second process verifies a count0 identity save, uses the same REST post and leaves one total CREATE.
- CREATE followed by a simulated application pointer commit rollback survives process exit; the second process creates nothing, prepares no image and reaches manual_review.
- The actual legacy repository/service path commits creation intent before POST; pointer rollback leaves that intent and blocks the next CREATE.
- A historical ambiguous row becomes manual_review while a later genuinely fresh article succeeds in the same bounded batch.
- Saved-ID recovery rejects wrong ID/type/trash/foreign origin, malformed JSON and HTTP404 without replacement creation.
- Focus/title/description readback failures are separate, and legacy SQL tests check their actual critical predicates.
- New INSERT establishes fresh state in both modes; duplicate UPDATE preserves old state.

These guarantees assume the corrective application protocol is in use. A conservative crash before POST can also consume freshness and require manual review; safety takes precedence over automatic replay. There is no automatic repair of an unknown remote outcome.

`publishing/wordpress/seo.py` is byte-identical to the reviewed REST implementation:
SHA256 `a637c9d38c684d10a5ed6c0f72b287a1a30bfc33e5285f387668f355a7338c3e`.
All three Yoast fields still use the existing authenticated session and independent exact GET readback. Canonical remains untouched. No direct WordPress SQL post/Yoast recovery is reintroduced. Unrelated media SQL remains unchanged.

## 5. Validation and files

| Final run | Passed / executed | Modules | Failures / errors / skips |
| --- | --- | --- | --- |
| Focused metadata + identity + idempotency + publishing + durable + security | 171 / 171 | 6 | 0 / 0 / 0 |
| Broader relevant application suite | 599 / 599 | 23 | 0 / 0 / 0 |
| Separate claimed-identity changed-row simulation | 11 / 11 | 1 class | 0 / 0 / 0 |

Focused module counts: metadata36, publication_identity17, publication_identity_idempotency23, publishing38, durable48, security9.

Separate MySQL changed-row simulation: **11/11 claimed-identity cases passed**, independently from the main suites (final receipt linked below). These use explicit MySQL-style zero changed rows and verify ownership/conflicts.
Before-correction diagnostic: one identical old-positional-API count0 case fails against the unchanged base repository with **one assertion failure and zero errors**. Evidence: `audit/corrective/evidence/rowcount_before_diagnostic_v2/summary.json`. The failure is the incorrect false return, rather than a new-signature incompatibility.
No errors from mismatched new method signatures are presented as reproduction evidence.

Authoritative evidence:
`audit/corrective/evidence/focused_v2/summary.json`,
`audit/corrective/evidence/broader_v2/summary.json`,
`audit/corrective/evidence/rowcount_current_v2/summary.json`,
`audit/corrective/final_verification_index.json`.
Earlier v1 receipts and the original 520/134/562 results remain separate. Final runs record stable source hashes, no unmocked external attempts and no skipped cases.

Recorded final commands (fresh phase names are required for any future rerun):

```powershell
& 'C:/Python313/python.exe' -I -S -B audit/corrective/run_tests.py --phase focused_v2 --modules GetNewsAPI.tests.test_yoast_metadata_sync GetNewsAPI.tests.test_publication_identity GetNewsAPI.tests.test_publication_identity_idempotency GetNewsAPI.tests.test_publishing GetNewsAPI.tests.test_durable_state GetNewsAPI.tests.test_provider_security
& 'C:/Python313/python.exe' -I -S -B audit/corrective/run_tests.py --phase broader_v2
& 'C:/Python313/python.exe' -I -S -B audit/corrective/run_tests.py --phase rowcount_current_v2 --modules GetNewsAPI.tests.test_publication_identity_idempotency.ClaimedIdentityIdempotencyTests
```

All tests use synthetic data, fake WordPress HTTP and isolated temporary/local fixtures. No runtime dependencies were installed. Local Python 3.13.3/packages differ from some deployment pins; the existing dependency receipt is preserved. Real disposable MariaDB integration and authenticated/rendered DEV checks were not run because this task authorizes neither live access nor environment provisioning.

Production files changed versus fixed DEV base (six):
`gpt_processor.py` (new-row publication provenance only),
`publishing/models.py`, `publishing/service.py`,
`publishing/wordpress/publisher.py`, `publishing/wordpress/seo.py`,
`repositories/publication.py`.

Tests changed/added (five):
`test_durable_state.py`, `test_publishing.py`,
`test_yoast_metadata_sync.py`, `test_publication_identity.py`,
new `test_publication_identity_idempotency.py`.

Unrelated quote changes in models and fixture strings/signatures/comments were restored to baseline formatting. Functional mappings, failure assertions and new safeguards remain. Misleading mapping/validation/rowcount comments were corrected.

Tracked diff versus base: **8 files, +951 / -334** (six production and two existing tests). Production alone: **+715 / -173**. Three new test files add **1,776 lines**, outside that tracked statistic. This report and diagnostic artifacts are untracked.

Exact short status:

```text
 M GetNewsAPI/gpt_processor.py
 M GetNewsAPI/publishing/models.py
 M GetNewsAPI/publishing/service.py
 M GetNewsAPI/publishing/wordpress/publisher.py
 M GetNewsAPI/publishing/wordpress/seo.py
 M GetNewsAPI/repositories/publication.py
 M GetNewsAPI/tests/test_durable_state.py
 M GetNewsAPI/tests/test_publishing.py
?? GetNewsAPI/tests/test_publication_identity.py
?? GetNewsAPI/tests/test_publication_identity_idempotency.py
?? GetNewsAPI/tests/test_yoast_metadata_sync.py
?? audit/
?? docs/audits/
```

`git diff --check` passes. The final cleanup restored 14 behavior-equivalent fixture-formatting lines and preserved their AST. No formatting-only source change remains in the original model fields.

The full review patch includes all production/test changes and this report:
`audit/corrective/corrected_fix.patch`.
Diff/hash/status checks are recorded in `audit/corrective/final_diff_review.json`.
Nothing is staged.

## 6. Preservation and prerequisites before separately authorized DEV testing

Original checkout branch/HEAD/status, local branch references and protected .env/log/image diffs remain unchanged. Both previous audit worktrees and their 371/209 recorded files remain intact. Original implementation/preflight/test artifacts are not overwritten. Preservation evidence: `audit/corrective/preservation_verification.json`.

Before deploying, verify the existing full Phase2 application schema and use a homogeneous publisher rollout: stop old publishing workers before emitting fresh rows. An old publisher does not consume this new provenance/intent protocol and could undermine its safety if run concurrently. Do not rerun migration/backfill to promote or erase historical state. Resolving manual_review rows needs a separate authorized bounded reconciliation task.

Remaining boundaries: real MariaDB transaction/lock verification, pinned runtime testing, authenticated WordPress metadata acceptance and rendered output remain unverified; media still depends on WordPress SQL. Legacy deferral will not steal an outstanding durable claim. Existing general retryable failure/lost-lease behavior is not redesigned.

Remote application/WordPress mutations: **none**. Real publishing/provider generation: **none**. Protected-file changes, commits, pushes, merges, deployments, infrastructure/configuration changes: **none**.

**READY FOR INDEPENDENT REVIEW.** Stop after local implementation, tests, diff review and this report.
