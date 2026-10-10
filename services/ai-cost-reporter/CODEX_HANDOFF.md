# Codex handoff — AI Cost Reporter

## Current architecture

This handoff supersedes the original OAuth-first setup instructions. The daily Dokploy Python job retrieves actual OpenAI organization and xAI team billing costs for a completed UTC day, generates PDF + JSON snapshots and securely POSTs them to a CoinCourier-owned Google Apps Script web app. The receiver creates `CoinCourier My Drive / AI Infrastructure Costs / YYYY / MM`. Schedule once daily at `09:00 Europe/Belgrade`, with one replica and persistent `/data/reports` storage.

The service lives at `coincourier-api/services/ai-cost-reporter/` in `github.com/branislav94/coincourier-api`, with its own image, requirements, entry point, protected environment and report volume. The original standalone source project remains intact. The public `.env.example` now documents Apps Script delivery plus optional legacy channels. Configure `GOOGLE_DRIVE_BACKEND=apps_script`, `GOOGLE_APPS_SCRIPT_WEB_APP_URL` and `GOOGLE_APPS_SCRIPT_SHARED_SECRET` explicitly in protected reporter settings; Python does not auto-load `.env`. To preserve existing installations, the code defaults to `oauth` when the backend variable is unset. The legacy OAuth uploader and Windows helper remain available separately; their original month-folder convention is `YYYY-MM`.

## Current status recorded on 2026-10-10

The user reports live TEST status `api_confirmed`: three authenticated successes, one rejected invalid-HMAC request, unchanged file IDs/hashes on retries, no billing calls and deleted temporary local artifacts. This is the user's helper result, recorded on the current client date; the execution date, nonce values and hash values were not supplied. No agent independently inspected Drive.

The reported [TEST folder](https://drive.google.com/drive/folders/131uquIdcNrqVymYjPsVJYhxAI61eM14l) contains the reported files `ai-cost-report-TEST-2026-10-06.pdf` and `ai-cost-report-TEST-2026-10-06.json`. **Independent owner inspection remains pending:** confirm the two files, visible TEST content and no duplicate copies. The owner should then manually set `AI_COST_TEST_UPLOADS_ENABLED=false` or remove it; this change is not confirmed.

The Google integration was published at `7786e1d`, following the earlier migration at `0de106d`. Current phases 1–3 authorize billing-accounting corrections, offline validation, preparation of a read-only billing smoke helper, then commit `Correct AI Cost Reporter billing accounting` and a non-force push to `origin/dev`. Publication receipts are reported separately, without predicting a new commit hash here. Keep the original `api-test` checkout and pending files untouched. Live provider requests, Dokploy deployment, production configuration/scheduling and further Google changes remain outside this authorization. No configured secret is read, printed, generated or rotated.

## Current billing-accounting corrections

- Provider numeric JSON decodes directly to Decimal from raw JSON, correcting the pre-existing float-decoding digit loss. Validated aggregation uses exact arithmetic independent of the ambient Decimal context; `usd_text` keeps full fixed-point decimal text in JSON/smoke summaries while PDF monetary display rounds cents only at presentation.
- OpenAI uses the official SDK-style bracketed array parameters. Scoped requests include project grouping and reject missing/null/unrequested result project IDs; unique exact daily UTC buckets and completed pagination are required for the complete month-to-date interval. Terminal `next_page` may be null or omitted only with explicit `has_more=false` and complete coverage.
- xAI uses the documented read-only historical analytics POST with exclusive next-day midnight end, `Etc/GMT`, USD SUM and daily granularity. Every unique description series must contain every requested UTC-midnight day exactly once; explicit `limitReached=false` is required. Empty collections, missing/duplicate/out-of-range days and truncated data fail closed.
- `scripts/billing_smoke_test.py [--date YYYY-MM-DD] [--live]` defaults to an offline plan without reading provider environment values or making requests. Explicit `--live` is prepared for a future separately approved check, using both providers through the same accounting code, fixed read-only endpoints, TLS verification and no redirects/environment proxy/netrc settings. It writes no reports, files or markers and performs no uploads, notifications, inference or billing mutations.
- The helper is included in the reporter image at `/app/scripts/billing_smoke_test.py`. A future approved container check uses `python /app/scripts/billing_smoke_test.py --date 2026-10-06 --live` through `docker exec`, with protected environment already configured. Current work does not run it live.
- A successful future check includes `generated_at_utc`, the local snapshot timestamp after both provider reads, along with exact daily/month-to-date totals and scopes. This does not establish provider billing finality; compare against console observations with their own timestamps.
- Live OpenAI and xAI status is pending. The synthetic Google result does not verify provider auth, query scope, monetary accuracy, completeness, billing latency or console reconciliation. README documents the future private environment setup, exact UTC daily/month-to-date comparison and delayed-billing procedure.

## Current billing-phase validation on 2026-10-10

The reviewed billing corrections passed offline checks before the authorized publication. The actual commit/push receipt is reported separately.

- Host Python: **106/106 passed**, including dedicated billing-correctness and read-only smoke tests. Separate Node receiver suite: **28/28 passed**.
- Docker image `ai-cost-reporter:billing-validation-20261010` built successfully, ID `sha256:fc45b1ddd82944b299401de41bf8f19a4886c1c6bcae98ed3997ddaddb372c97`. Its suite discovered 106 tests: **103 passed, three Node-dependent tests skipped**; those three passed on the host.
- With networking disabled, the bundled smoke helper's default offline plan and non-root demo passed. The unchanged `sleep infinity` command kept the container running for `docker exec`. The exact production command failed safely on absent billing keys before provider access.
- Independent GetNewsAPI regression validation passed **505 tests with 62 infrastructure-dependent skips**. All **148 public source-file hashes** matched the existing application; its functions, dependencies, database/Compose and production configuration remain unchanged.
- No live OpenAI/xAI requests, credential inspection/generation, new Google operation, Dokploy deployment or production scheduling was performed. Live provider acceptance and console reconciliation remain pending separate approval.

The following older integration results are retained as historical context.

## Historical Google-integration verification on 2026-10-10

The reviewed integration passed the full offline rerun for the authorized publication. These checks validate the changes; the actual commit/push receipt is reported separately.

- All 61 Python tests and all 28 mocked Node receiver scenarios passed.
- The isolated Docker suite passed 58 tests and skipped three Node-dependent tests, which passed on the host.
- Independent GetNewsAPI regression validation passed 505 tests with 62 infrastructure-dependent skips. Existing news application sources and configuration remain unchanged.
- At that integration stage billing code was preserved. Its read-only audit identified float-decoding and array-wire compatibility issues subsequently corrected by the current billing-accounting work. Live billing and project-filter acceptance remain unverified.

## Historical offline TEST preparation on 2026-10-09

These checks preceded the user-reported live result. At that time the changes were local on `add-ai-cost-reporter-dev-20261009` with worktree HEAD `0de106d`; no new commit, push, Google modification, Dokploy deployment or billing request was made. Remote CI was not queried for those local changes.

- All 61 host Python tests and all 28 Node receiver scenarios passed using mocked network and Google services.
- The rebuilt isolated Docker image ran 61 Python tests: 58 passed and three Node-dependent tests were skipped. Those three passed on the host.
- With networking disabled, the unprivileged reporter ran the demo through `docker exec` while the container's unchanged default `sleep infinity` command kept it running. Generated PDF/JSON files in a named report volume survived container recreation. Temporary test containers and volumes were removed afterward.
- The service `.dockerignore` excludes both `.venv` and `venv`, matching Git exclusions and keeping local environments outside its isolated build context. Existing root GetNewsAPI configuration remains unchanged.
- Independent GetNewsAPI validation passed 505 tests with 62 infrastructure-dependent skips, using a snapshot whose 148 public source-file hashes matched the existing application. No databases or provider endpoints were contacted.
- The synthetic helper passed offline. Explicit `--live` with no process secret failed before network access. No Google POST was performed during this offline preparation phase; the later user-reported live result appears above.

## Migration boundaries and paths

- No credentials, `.env`, virtual environments, generated reports/markers or Git metadata were copied. The retained `.env.example` is a template only. Do not copy or expose the source project's local/private artifacts.
- Run all reporter Python/Node test and local demo commands from `services/ai-cost-reporter/`, using this service's requirements in a separate Python environment. The existing news entry point is `GetNewsAPI/app.py`.
- Reporter CI is `.github/workflows/ai-cost-reporter-offline.yml` at the repository root, with service working directory and service-specific requirements cache input. The standalone GitHub publication script is omitted because it would initialize/publish a nested repository. The legacy OAuth consent helper is retained.
- GetNewsAPI functions, databases, dependencies, migrations, Compose files and production configuration remain unchanged. The existing root Dockerfile copies only `GetNewsAPI/` and specific `maintenance/` paths, so no root Dockerfile or `.dockerignore` change is needed.
- Future reporter deployment is a separate Dokploy Application using Repository `branislav94/coincourier-api`, Branch `dev`, repository build path `/`, Dockerfile Path `services/ai-cost-reporter/Dockerfile` and Docker Context Path `services/ai-cost-reporter`. No existing news deployment is changed. Keep one reporter replica, a separate persistent `/data/reports` volume, no incoming port and scheduled command `python /app/app.py`.

## Delivery-verification and publication boundaries

Current work publishes reviewed billing-accounting corrections and offline smoke preparation after the completed Google integration. The user authorizes the stated commit and non-force push to `origin/dev`, while Dokploy deployment, production configuration changes and OpenAI/xAI billing requests remain separately authorized actions. The original `api-test` checkout, current branch and pending log/diff files remain untouched; never display their contents, stash, reset, modify or commit them. Private fingerprints may verify preservation.

The receiver's TEST extension retains protocol v1. It uses signed `ai-cost-report-TEST-YYYY-MM-DD.pdf/json` filenames and JSON `data_type=SYNTHETIC_DELIVERY_TEST` with `test_namespace`, bound by the signed raw JSON hash. Normal actual-report semantics and ordinary demo rejection remain unchanged. TEST requires `replace=false` and both Google Script Properties `AI_COST_TEST_UPLOADS_ENABLED=true` and `AI_COST_TEST_PARENT_FOLDER_ID`. Files go only to the dedicated private parent at `AI Cost Reporter Tests/<namespace>/YYYY/MM`; there is no production-parent or My Drive-root fallback.

`scripts/test_apps_script_upload.py` defaults offline; its explicit `--live` mode performs valid upload, fresh-authentication duplicate retry, invalid-HMAC rejection and final unchanged-ID check. Artifacts are deterministic for the same namespace/date and remain in temporary local storage only. The helper reads no `.env`, imports no daily billing job and requires no provider keys. The shared secret is entered privately through `GOOGLE_APPS_SCRIPT_SHARED_SECRET`, never a CLI argument, logged value or saved test secret file. Responses must confirm `mode=test`, namespace, folder ID and both artifact identities/hashes.

**Remaining Google actions:** independently inspect the reported TEST folder/files and manually disable TEST uploads afterward. Neither action is confirmed by the helper's response validation. The reported live result supersedes the earlier missing-secret/setup blocker; do not recreate the deployment or rotate its configured secret for publication. Any later Google change must be explained and separately authorized. README retains the guarded test procedure and private PowerShell prompt/cleanup block for future authorized checks.

## Historical CoinCourier migration verification on 2026-10-09

These results describe the earlier migration published at `0de106d`; subsequent offline preparation and the user-reported live result appear above.

- The latest 19 reporter source/configuration/documentation/test files were copied from the reviewed GamingNewsAPI service, along with its dedicated root CI workflow. All Python ASTs match the source; only inherited trailing whitespace and migration documentation changed. Apps Script files, Dockerfile and dependency manifests remain unchanged.
- Reporter validation passed in the isolated CoinCourier worktree: 45 Python tests, 20 actual receiver scenarios with Google services mocked, import-origin checks and synthetic PDF/JSON generation.
- Reporter Docker build passed with `services/ai-cost-reporter` as context. The network-disabled container passed 43 tests and skipped two Node-dependent tests already passed on the host. Its unchanged running-container command accepted the demo through `docker exec`; output was generated as a non-root user, with no GetNewsAPI, credentials or Git files in the image.
- Independent GetNewsAPI regression validation used its exact pinned requirements and a read-only public source snapshot in a network-disabled Python 3.11 container. It passed 505 tests; 62 MariaDB/Compose infrastructure checks were skipped. Existing dotenv isolation was active, and no databases or live provider endpoints were contacted.
- Existing GetNewsAPI source, runtime dependencies, root Dockerfile, root ignore rules, Compose files and deployment configuration were untouched. The migration published only the new service and its new CI workflow.
- Publication and guarded cleanup receipts were reported separately. These were local migration checks and did not establish real billing or Drive integration behavior. Validation scratch files and test images remained outside the published service tree.

## Implemented delivery components

- `apps_script_delivery.py`: bounded PDF/JSON artifacts; exact UTF-8 HMAC-SHA256 protocol; body signature; fresh timestamp/nonce per attempt; restricted HTTPS `/exec` endpoint and ContentService response redirect; streamed bounded response and strict success validation.
- `google_apps_script/Code.gs`: authenticated `doPost(e)`; ±300-second timestamp window; durable nonce storage with expiry and capacity bounds; script lock; complete payload/date/name/MIME/hash checks before Drive access; private folder hierarchy; identical-file reuse; partial-pair recovery; explicit conflicts; file-ID-preserving force replacement through Advanced Drive v3. `doGet` never accesses Drive or discloses stored data.
- `google_apps_script/appsscript.json`: V8 runtime, Advanced Drive v3 service and full Drive owner scope. Manual setup requires CoinCourier owner consent, execute-as-owner deployment and anonymous callers.
- `app.py`: selectable delivery backend, destination-aware markers, preserved report snapshots after partial failures, and durable force-replacement intent.
- Offline Python tests and the Node `Code.gs` harness cover signing, success/rejection bodies, malformed responses, replay, integrity, duplicates, partial delivery and replacement. Google service doubles make no live calls.
- `README.md` provides exact Script Properties, manifest, deployment, Dokploy, protocol, TEST and retry instructions. `.env.example` documents complete backend/channel settings. No earlier Google OAuth secrets are needed for Apps Script.

The configured shared secret is exactly 64 lowercase hex characters, kept in Apps Script `AI_COST_SHARED_SECRET` and protected Dokploy `GOOGLE_APPS_SCRIPT_SHARED_SECRET`. The key is literal UTF-8 text, not decoded hexadecimal bytes. Do not request, read, print, log or commit it. Publication does not generate or rotate it. The private helper prompt uses only a temporary process environment value and clears it afterward.

## Local verification completed on 2026-10-07

These results belong to the original standalone source project before migration.

- Baseline: 19 existing tests passed, plus the synthetic PDF/JSON demo.
- Final: 45 Python tests passed on Windows Python 3.13, including Python-signed requests executed through the actual receiver with Google services mocked. All 20 Node receiver scenarios passed.
- The final Docker image built successfully on Python 3.12. With networking disabled, 43 Python tests passed and two Node-dependent tests were skipped (already passed on the host). The default running container accepted the Dokploy-style `docker exec` demo command and generated PDF/JSON as the unprivileged reporter user.
- Billing fetchers, report schema/aggregation and PDF definitions were compared against the original Git version and remained unchanged. Source-only secret-pattern scans and `git diff --check` passed; no files were staged, committed or pushed.
- No real credentials were read/generated, no provider/Google account API calls were made, and no production deployment or scheduling was performed. The local `.venv` and synthetic reports are ignored artifacts.

## Billing implementation and open accuracy checks

Daily/month-to-date actual provider USD reporting, explicit project/team scope and complete UTC days remain the accounting semantics. Both provider responses must succeed and pass completeness checks. UTC report days are independent of the job's Serbian execution timezone; billing may be delayed or adjusted.

The earlier default `response.json()` float decoding is replaced with raw JSON Decimal parsing. Exact aggregation prevents source digit loss before reporting and avoids ambient Decimal-context rounding. Monetary schema/currency/non-finite checks fail explicitly. This is verified offline, not by querying real billing.

The [official OpenAI Costs reference](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs) defines project/group arrays and an inclusive start/exclusive end. The updated request uses the official SDK's bracketed names (`project_ids[]` and `group_by[]`) and scoped project grouping/identity checks. SDK-aligned serialization is verified offline; server acceptance and live project-scoped accuracy remain unverified.

The [full xAI billing schema](https://docs.x.ai/developers/rest-api-reference/management/billing.md) explicitly defines exclusive `endTime` and dense points. A complete day therefore ends at the next midnight, rather than `23:59:59`. Empty `timeSeries` is not documented as verified zero, so the current policy treats it as unknown and fails closed. Use minimum team-scoped billing-read access and verify the actual permission names offered by the current console.

After separate approval, use existing newly rotated provider keys privately and run the helper for a complete day. Compare exact daily/month-to-date amounts with both consoles using the same UTC periods and project/team scope, not purchases, balances or tax-inclusive invoices. Capture the observation timestamp and recheck delayed billing. Preserve existing snapshots; a deliberate replacement uses separately approved `--force-resend`. No provider live result or console reconciliation is claimed yet.

## Next verification steps

1. From `services/ai-cost-reporter/`, read `AGENTS.md`, `README.md`, `app.py`, both delivery modules, receiver and manifest. Run:

   ```bash
   python -m unittest discover -s tests -v
   node tests/test_apps_script_receiver.cjs
   python app.py --demo --date 2026-10-06 --output-dir ./reports
   ```

2. Inspect the resulting synthetic PDF and JSON. Keep reports and markers ignored by Git/Docker. `--no-send` queries live providers and must not be used as an offline check. For a separately authorized live preview, use `--no-send --output-dir ./reports/preview`; an existing marked snapshot cannot be overwritten by that mode.
3. From the CoinCourier API repository root, build with `docker build -f services/ai-cost-reporter/Dockerfile -t ai-cost-reporter:local services/ai-cost-reporter`. Run its offline container demo with networking disabled; preserve the unchanged `python /app/app.py` scheduled command in its independent application. Verify Dokploy's installed scheduler supports `Europe/Belgrade` and displays the expected next local 09:00 execution before enabling its schedule.
4. The user-reported TEST result already establishes helper response validation for that invocation. Keep the existing script editor, secret and target folder private; no new receiver deployment or setup is part of publication. Future manual Google changes require explanation and separate authorization. No Desktop OAuth JSON is used.
5. The owner must independently inspect the reported TEST PDF/JSON and confirm no duplicates, then manually disable/remove `AI_COST_TEST_UPLOADS_ENABLED`. These actions remain unconfirmed. Later separately authorized TEST checks use the documented guarded helper, dedicated namespace and `replace=false`. Node doubles cover partial failures and other protocol cases but do not establish Google concurrency or quotas.
6. Current work prepares the billing helper but makes no provider requests. After separate live approval, use existing newly rotated keys supplied privately through process environment only; do not read credential files or generate/retrieve keys. Run `scripts/billing_smoke_test.py --date 2026-10-06 --live` to verify BOTH providers, then compare exact daily/month-to-date UTC amounts and scope with the consoles. Log only sanitized status/amount summaries. Previously disclosed keys must be revoked/replaced; do not assert live compatibility from offline tests.
7. Complete the authorized publication with commit message `Correct AI Cost Reporter billing accounting` and a non-force push to `origin/dev`, after staged-file review and credential-safe scans. Report the actual publication receipt separately. Do not deploy Dokploy, enable a production schedule, alter GetNewsAPI or query billing providers.

## Operational details

- PDF limit: 2 MiB; JSON: 512 KiB; request: 4 MiB; response: 64 KiB. Nonces are retained through signed timestamp +300 seconds, pruned after expiry and capped at 512 live records.
- An exact replay is rejected even if the first request partially failed. Retry the original artifacts with fresh authentication; ordinary reruns preserve the stored snapshot. Pending force-replacement intent applies only to the same backend and destination.
- Same-name matching bytes are reused. Different bytes require explicit `--force-resend`; ambiguous folders/files and wrong MIME types fail. Separate Drive writes can leave a partial pair until the next successful retry.
- Backend and destination-hash marker matching avoids skipping a newly configured endpoint. A parent-folder change behind the same endpoint needs deliberate resend for previously delivered dates. Local markers do not detect remote deletion or external modification.
- Google returns application errors in JSON, potentially with HTTP 200. Both file identities and hashes must match before recording local success. Never include raw server exception text or request bodies in logs.
- Retain one reporter replica and the persistent report volume; do not bake configuration or OAuth credentials into the image. Optional notifications retain their existing behavior.

## Definition of done before production

- Offline Python and receiver tests pass, and the synthetic PDF renders clearly.
- Credential/report exclusions are verified in Git and Docker; no disclosed key is reused.
- Docker dependencies and exact scheduled command are validated.
- The user-reported TEST helper result is `api_confirmed`; independent owner inspection of private files/no duplicates and manual TEST-gate disabling are still required.
- Authorized provider smoke tests validate both APIs, project-filter encoding, team scope and complete UTC periods.
- Production deployment and one daily timezone-correct schedule are explicitly authorized; reruns complete partial uploads without duplicate files.

Repository privacy, remote CI status, Google account consent, real API behavior and production deployment are external state. Verify them when authorized; do not infer completion from this handoff.
