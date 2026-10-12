# GetNewsAPI: confirmed Yoast metadata regression fix

Local implementation and verification completed on 2026-10-12. Nothing was deployed, published, committed, merged or pushed.

## Baseline and evidence

Repository: `branislav94/coincourier-api`.
Fresh `git fetch origin` succeeded at 17:25:35 UTC. Latest fetched `origin/dev` and the fixed implementation baseline are `e1ac63b14c9670df0b7e53656ba26559af9f5940`; fetched `origin/main` is `349a4233880632d1c125c7f9dfec66b0a6bf1c0c`. Local branches, including local main, were preserved.

Implementation worktree:
`C:\Users\Win11\Documents\GitHub\api-test-yoast-confirmed-fix-20261012-192711` (detached HEAD).

The user's confirmed evidence covers DEV posts **5479, 5477, 5475, 5473 and 5471**: existing posts expose Yoast title/schema/OpenGraph but omit `yoast_head_json.description`, despite a valid application-side generated description. The user also confirmed that available WordPress SQL views can be stale. The earlier public preflight independently recorded absent rendered descriptions for all five, and registered scalar-string REST fields for ordinary posts:

`_yoast_wpseo_focuskw`, `_yoast_wpseo_title`, `_yoast_wpseo_metadesc`.

Existing evidence: `../api-test-yoast-metadata-fix-20261012-084658/audit/preflight-20261012/public/contract-summary.json`. That preflight advertised WordPress 7.1.3 / Yoast 27.9, but authenticated edit/write acceptance and deployed application SHA were unknown. No additional remote request or authenticated write was made in this implementation task. Successful fakes do not establish restored public descriptions or changed Yoast scores.

The prior local REST implementation was reused and reviewed. This task corrected its remaining reliance on WordPress SQL for post identity/recovery and its canonical handling, rather than adding a second publishing implementation.

## Root cause and behavior

Before the fix, `publishing/wordpress/seo.py:write_yoast_metadata` wrote directly to a separately configured WordPress database, discovered a prefix from an arbitrary options-table match, and issued SQL postmeta upserts. It clipped descriptions to 160 characters and titles to 58. It never authenticated to the owning REST instance or independently verified its stored fields. A successful SQL operation therefore could not establish metadata delivery to the site serving the post. Where postmeta lacks uniqueness on `(post_id, meta_key)`, that SQL statement also does not guarantee a single authoritative row.

The publisher used that path after creating the REST post. It consulted the same WordPress SQL view to recover saved IDs/publication markers, and the legacy branch did not durably save returned IDs in the application. A metadata failure could leave a real post with incomplete SEO and permit another creation on retry.

After the fix:

1. Validate nonempty focus, SEO title and description without altering their values.
2. Preserve native creation fields and scheduling/taxonomy/image behavior.
3. After creation, save the returned post ID/URL in the application before attempting metadata synchronization.
4. POST the three fields to the same configured authenticated WordPress post endpoint.
5. Independently GET that exact post in edit context and compare its numeric ID and every intended string exactly.
6. Mark publication complete only after verification. Return explicit partial failure with the known ID/URL otherwise.
7. Retry an application-saved post ID through its owning REST endpoint before image preparation or post creation. Known-ID lookup/auth/site/metadata failure stays a failure and cannot fall through to a new post.

Native title, optional excerpt, HTML and SEO fields remain distinct:

| Application/DTO field | WordPress destination |
| --- | --- |
| `title` | Native post title |
| `full_text` / `html_content` | Native content |
| Optional `excerpt` | Native excerpt |
| `seo_focus` | `meta._yoast_wpseo_focuskw` |
| Optional `seo_title`; otherwise complete `title` | `meta._yoast_wpseo_title` |
| `seo_meta` / `seo_description` | `meta._yoast_wpseo_metadesc` |
| `seo_slug` | Existing native slug path, unchanged |
| `seo_canonical` | Unchanged; outside supported three-field contract |

There is no publisher truncation, excerpt/content fallback for an available description, title/description interchange, score fabrication or Yoast-score recomputation.

## REST verification and retry scope

The existing authenticated Requests session, `WP_API_URL`, and configured connection/read timeouts are reused. The metadata POST body is only `{'meta': {three exact fields}}`. A HTTP 200 write triggers a separate GET to `/wp-json/wp/v2/posts/{id}` with `context=edit` and `_fields=id,meta`. The GET must return HTTP 200, a JSON object, the exact integer post ID, and all three scalar strings equal to the intended values. The POST's response ID is also checked; its echoed metadata is not used as storage proof.

Missing fields, mismatches, invalid JSON/IDs, rejected writes/reads and transport errors return controlled diagnostic codes. Logs identify the post, stage, offending field or status without credentials, remote error bodies or full article values. HTTP 401/403, an accepted write with missing description, and a post created before metadata timeout are distinct test cases.

Durable publication keeps the existing claim-token-protected application save/fail/complete logic. The legacy branch now reads and saves the application's existing `wp_post_id` / `wp_post_url` columns, guards against overwriting another ID and verifies the pointer in the transaction before committing. Those columns already appear in `maintenance/migrations/002_phase2_durable_state.sql`; **no migration is introduced or executed**. If unavailable, legacy publication fails visibly before image work or remote creation.

A saved URL must match the configured site's origin; authenticated REST recovery also checks exact ID, ordinary post type, usable status and same-site returned link. Recovery updates SEO on that post, leaving its native content and URL unchanged.

**Duplicate guarantee:** a metadata failure after the application pointer commits reuses the same post on subsequent retries, including a fresh process. There is no universal exactly-once guarantee. Creation whose response is lost, process death before ID commit, an application commit failure, or an old post with no trustworthy application pointer can still leave an unlinked post and allow later duplication. Removing stale SQL marker recovery makes this limitation explicit; it does not repair old linkage automatically. Metadata failure after a committed pointer is the demonstrated safe path.

Canonical is not sent or read back: the existing public contract does not register it. A supplied canonical produces a sanitized unchanged-field warning and does not block the supported three fields. Existing canonical values are preserved.

Direct WordPress DB dependencies remain for durable media existence/marker lookup/persistence and existing media/image helpers. Unused legacy post-marker helpers remain for compatibility but are not invoked by the repaired post/Yoast path. Application SQL is required for identity and publication state. No DB names, topology, settings or feature flags were changed.

## Files and verification

Production files changed (five):

- `GetNewsAPI/publishing/models.py`: independent optional SEO title/excerpt and captured existing URL.
- `GetNewsAPI/publishing/service.py`: pass the saved post URL to recovery.
- `GetNewsAPI/publishing/wordpress/seo.py`: authenticated three-field update and exact independent readback.
- `GetNewsAPI/publishing/wordpress/publisher.py`: REST post recovery, safe partial results, native field separation and legacy pointer callback.
- `GetNewsAPI/repositories/publication.py`: bounded legacy pointer read/save with conflict protection and transaction verification.

Tests changed/added (four):

- `test_publishing.py` and `test_durable_state.py`: adapt fixtures to the actual REST contract; preserve and strengthen publishing/retry assertions.
- New `test_yoast_metadata_sync.py`: 27 regressions for exact fields/serialization, Unicode/NBSP/punctuation/dollars, separate title/excerpt, canonical preservation, HTTP/auth/readback failures, no WordPress SQL, same-ID durable/legacy retry and process restart.
- New `test_publication_identity.py`: 14 fake application-DB tests for missing rows/schema, invalid IDs, idempotent saves, conflicts, URL behavior, readback/commit failures and reopened state.

The restart regression launches **two distinct Python processes** with the actual repository, PublishingService and WordPressPublisher. An isolated file-backed SQLite adapter and JSON fake WordPress store survive process exit. Process one creates post 77, commits the application pointer, then times out on metadata. Process two reopens that state, succeeds on the saved REST post, and leaves one total creation with no repeated image preparation. This proves process persistence/control flow with local fixtures; it is not a real MariaDB/WordPress integration run.

| Run | Passed / executed | Modules | Failures / errors / skips |
| --- | --- | --- | --- |
| Untouched fetched DEV baseline | 520 / 520 | 20 | 0 / 0 / 0 |
| Final focused: metadata 27 + identity 14 + publishing 37 + durable 47 + security 9 | 134 / 134 | 5 | 0 / 0 / 0 |
| Final broader application suite | 562 / 562 | 22 | 0 / 0 / 0 |

Run evidence: `audit/confirmed_tests_broader_baseline_corrected/summary.json`; `audit/confirmed_tests_focused_after_final2/summary.json`; `audit/confirmed_tests_broader_after_final2/summary.json`. Source hashes remained unchanged during each accepted run and external connection attempts were zero. Each module runs in a fresh `python -I -S -B` process, with fake environment values, dotenv/file logging suppressed, and guards for real HTTP, DNS, sockets and MySQL. Only validated Windows stdlib internal asyncio socketpairs are allowed.

The final 27 metadata regressions were also run against a Python-only snapshot of the unchanged fetched DEV implementation: **0/27 passed**, with 23 test cases reporting assertion failures and four reporting errors (31 total failure/error events including subtests). No external connection attempt occurred. These are intentionally unsuccessful regression diagnostics, separate from the passing original baseline suite. Evidence: `audit/before_source_provenance.json` and `audit/before_source/audit/confirmed_tests_metadata_before_corrected/summary.json`. They demonstrate missing contract/retry behavior in old code; error cases also expose newly required API interfaces.

Baseline initial harness errors caused by blocking Windows' internal socketpairs were superseded by the corrected untouched-baseline result, not by changing application code or assertions. Intermediate fixture-debug runs are retained separately; the final results above are authoritative.

No runtime dependencies were installed or updated. Tests use local Python 3.13.3 and installed packages, which differ from some repository pins; exact versions are in `audit/confirmed_local_dependencies.json`. Opt-in disposable MariaDB integration tests were not run because no test database was provisioned. The unrelated ai-cost-reporter service suite was outside scope.

## Diff, preservation and remaining deployment checks

`git diff --check` passes. Tracked diff: **7 files, +647 / -351**. This excludes the two new test files (807 and 220 lines), this report and untracked diagnostic artifacts.

Exact short status at completion:

```text
 M GetNewsAPI/publishing/models.py
 M GetNewsAPI/publishing/service.py
 M GetNewsAPI/publishing/wordpress/publisher.py
 M GetNewsAPI/publishing/wordpress/seo.py
 M GetNewsAPI/repositories/publication.py
 M GetNewsAPI/tests/test_durable_state.py
 M GetNewsAPI/tests/test_publishing.py
?? GetNewsAPI/tests/test_publication_identity.py
?? GetNewsAPI/tests/test_yoast_metadata_sync.py
?? audit/
?? docs/audits/
```

Nothing is staged. Original checkout branch/HEAD/status and all local branch refs are unchanged. The four protected files retain their SHA256, length and modification timestamps. Both earlier worktrees retain their HEAD/status/file sets and all 371 / 209 recorded file hashes. Evidence: `audit/implementation_baseline.json`, `audit/preservation_verification.json`, `audit/final_diff_review.json`, `audit/confirmed_fix.patch`.

Independent diff review found no blocking issue. Generation, prompts, slug generation, scheduler, image-search source, DB/deployment configuration and main are unchanged. Prior audit/preflight reports and collectors are preserved.

Before any separately authorized DEV deployment, verify existing application pointer columns, authenticated ordinary-post edit capability, and acceptance/readback of all three registered fields on the actual owning instance. Then inspect the rendered description after a bounded authorized publication. Media's remaining SQL boundary and pre-pointer-commit creation failures retain their existing limitations. Existing affected posts require a separate bounded repair task using trusted application IDs and preserving manually edited content and publication identity; no existing post was repaired here.

Remote application/WordPress mutations: **none**. Real publishing/provider calls: **none**. Infrastructure changes, commits, pushes, merges and deployments: **none**. Work stops after this local implementation, tests, review and report.
