# Codex handoff — AI Cost Reporter

## Current architecture

This handoff supersedes the original OAuth-first setup instructions. The daily Dokploy Python job retrieves actual OpenAI organization and xAI team billing costs for a completed UTC day, generates PDF + JSON snapshots and securely POSTs them to a CoinCourier-owned Google Apps Script web app. The receiver creates `CoinCourier My Drive / AI Infrastructure Costs / YYYY / MM`. Schedule once daily at `09:00 Europe/Belgrade`, with one replica and persistent `/data/reports` storage.

The service lives at `coincourier-api/services/ai-cost-reporter/` in `github.com/branislav94/coincourier-api`, with its own image, requirements, entry point, protected environment and report volume. The original standalone source project remains intact. The public `.env.example` now documents Apps Script delivery plus optional legacy channels. Configure `GOOGLE_DRIVE_BACKEND=apps_script`, `GOOGLE_APPS_SCRIPT_WEB_APP_URL` and `GOOGLE_APPS_SCRIPT_SHARED_SECRET` explicitly in protected reporter settings; Python does not auto-load `.env`. To preserve existing installations, the code defaults to `oauth` when the backend variable is unset. The legacy OAuth uploader and Windows helper remain available separately; their original month-folder convention is `YYYY-MM`.

## Current status recorded on 2026-10-10

The user reports live TEST status `api_confirmed`: three authenticated successes, one rejected invalid-HMAC request, unchanged file IDs/hashes on retries, no billing calls and deleted temporary local artifacts. This is the user's helper result, recorded on the current client date; the execution date, nonce values and hash values were not supplied. No agent independently inspected Drive.

The reported [TEST folder](https://drive.google.com/drive/folders/131uquIdcNrqVymYjPsVJYhxAI61eM14l) contains the reported files `ai-cost-report-TEST-2026-10-06.pdf` and `ai-cost-report-TEST-2026-10-06.json`. **Independent owner inspection remains pending:** confirm the two files, visible TEST content and no duplicate copies. The owner should then manually set `AI_COST_TEST_UPLOADS_ENABLED=false` or remove it; this change is not confirmed.

Google delivery was published at `7786e1d`, following the migration at `0de106d`; billing-accounting corrections were published at `94507895cf460f3ec215a2f011a61ed5b0ff24e4`. The user now authorizes final reporter-only commit and non-force publication to CoinCourier `origin/dev` after verification. Publication excludes Dokploy deployment/scheduling, Google deployment/property changes, new agent live calls and report-generation jobs. Keep the original `api-test` checkout and pending files untouched. Preserve the existing Apps Script deployment and configured secret; no credential is inspected, generated or rotated. The actual final publication receipt is reported separately; do not predict its commit hash or claim remote CI before checking it.

## Current xAI evidence and accounting-state status

The user reports final corrected xAI live verification passed: Management key accepted and team linkage matched; grouped and ungrouped billing HTTP 200; billing status `recorded_usage_absent` with `accounting_state=NO_RECORDED_USAGE`, recorded daily/month-to-date USD 0 and `reconciliation_status=not_reconciled`; overall `diagnostics_recorded_usage_absent` with exit code 0. The user reports dashboard zeros for the corresponding period. This is the user's live helper result, not agent inspection of the provider account/responses. Execution date, exact report date and raw schema details were not supplied and are not invented. The result does not establish fully reconciled zero consumption, a universal empty-response guarantee or successful OpenAI live/project-scope accounting.

Console timezone/filter settings were not independently inspected by the agent. The user reports a corresponding dashboard period; examples using completed October 6 are future recheck instructions, not the final verification's asserted report date. Later separately authorized rechecks should compare identical UTC daily/month-to-date intervals and team scope.

The local change permits one additional same-endpoint xAI read only after an explicit valid empty grouped response with explicit `limitReached=false`. The confirmation changes only `groupBy` to `[]`, preserving team, UTC month-to-date period, USD SUM, daily buckets and empty filters. Explicit zero spending still requires exactly one ungrouped series with every requested UTC-midnight day exactly once and finite numeric zero at every point. Nonzero or cancelling positive/refund values fail `aggregation_mismatch` at coverage. Partial nonempty grouped records never trigger confirmation.

Two valid explicit empty datasets with explicit false truncation and identical request scope produce `NO_RECORDED_USAGE`, not explicit zero-spending records. Each empty envelope must have exactly the expected `timeSeries` and `limitReached` keys; unknown scope/error/cursor fields are rejected. Recorded amounts are 0 USD as of the observation, with no invented daily points or model breakdown. Failed/missing/malformed/partial/truncated/conflicting reads remain `UNKNOWN` and block reporting. Complete explicit recorded zero/nonzero results retain `RECORDED_SPENDING`.

The inactive provider JSON adds `accounting_state="NO_RECORDED_USAGE"`, `accounting_evidence="grouped_and_ungrouped_empty"`, `reconciliation_status="not_reconciled"` and `allow_recheck=true`, together with exact recorded-zero amounts and an empty breakdown. These combinations are validated on snapshot reuse. PDF shows xAI no recorded usage, `RECORDED TOTAL` and no-records/late-billing/recheck caveats; optional email uses the same qualified notes. Normal financial/demo JSON stays compatible; explicit dense zero retains the earlier `ungrouped_dense_zero_confirmation` evidence.

Inactive diagnostic output uses check `status=recorded_usage_absent`, `accounting_state=NO_RECORDED_USAGE`, usage states `no_recorded_usage`, coverage `valid_empty_datasets`, attempted confirmation true and confirmed zero false, plus nonreconciliation/recheck flags. Accepted selected checks yield `diagnostics_recorded_usage_absent` and exit 0; explicit record checks retain `diagnostics_confirmed`. Legacy inactive smoke uses `recorded_spending_snapshot` with provider/top-level caveats rather than fully confirmed consumption. `response_schemas` summarize only xAI billing POSTs with whitelisted types/counts, JSON status/query mode and metadata-completeness flags, never raw field names/values/labels/headers/private IDs.

The diagnostic permits at most one key-validation GET and two billing POSTs. The [official expanded schema](https://docs.x.ai/developers/rest-api-reference/management/billing.md) documents grouping and dense points in existing series, but does not define empty as final zero or echo team/period/metric/timezone scope. `NO_RECORDED_USAGE` is the user's explicitly authorized application interpretation of successful record absence. Provenance refers to the validated same request scope, not independent response-echo verification. The corrected classification is user-reported live-verified for the checked period; final reconciliation and future response guarantees remain unresolved.

- `--diagnostic --live --provider both|openai|xai` prepares independent OpenAI cost, xAI Management-key validation and xAI billing checks. `--diagnostic` without `--live` remains offline and reads no environment values. Legacy `--live` retains complete both-provider accounting.
- The xAI-only offline plan names only the Management-key/team settings and describes the conditional ungrouped confirmation without reading values or dispatching any request.
- Diagnostic output allows only structured request counts/statuses, failure stage/category/code, schema/completeness flags, missing UTC days, pagination and whitelisted response-schema types/counts. Complete explicit amounts retain exact fixed-point USD text; validated absence carries only recorded-zero amounts and its distinct caveats, while unknown amounts remain unknown. Diagnostic results never contain a combined total.
- `confirmed_zero` confirms only a covered USD cost total of zero; it does not prove zero requests or zero token usage.
- Request counts record dispatch attempts and do not prove provider receipt. `http_statuses` retains a status or null for each attempt; `http_status` is the final attempt's status, including null after a timeout following an earlier HTTP 200.
- The additional read-only endpoint is `GET https://management-api.x.ai/auth/management-keys/validation`. [xAI's guide](https://docs.x.ai/developers/management-api-guide) says it requires no ACL permission and returns metadata; [the schema](https://docs.x.ai/developers/rest-api-reference/management/auth.md) uses `apiKeyId`, `scope`, `scopeId` and deprecated `teamId`. A valid bounded response must identify a key and validate present scope/ACL types; print only scope/team-linkage classifications, never metadata, key IDs, names, owner details or raw/redacted key values.
- Team-scoped `scopeId` can be compared with the configured team. The helper emits only `key_scope` (team/organization/unspecified/unknown) and `team_linkage` (match/mismatch/unknown/not_configured). Organization scope does not prove individual team membership; missing team configuration still permits the key check while billing preflight fails. An accepted key for a mismatched team remains `key_accepted=true` but its validation check fails with `scope_mismatch`. Validation acceptance does not prove billing permission, and billing is checked independently even after key-validation rejection.
- Inference metadata uses a different host and snake-case schema at `https://api.x.ai/v1/api-key`. The Management key must stay on its Management host; a token prefix cannot prove key type.
- The current organization-wide OpenAI path omits project filters when configuration is absent and uses line-item grouping, documented daily UTC bounds and pagination. Official documentation/source inspection did not establish a clear defect in this unfiltered path. Strict coverage checks remain intentional; investigate the actual failure category before changing accounting.
- README contains the local private xAI-only PowerShell diagnostic procedure: secure prompts for the Management key and team ID, `--provider xai`, and BSTR/environment cleanup. It records the process exit code without throwing over diagnostic JSON and leaves OpenAI settings untouched. The helper never overrides configured project filters.

Final offline publication verification on 2026-10-10 passed:

- Host Python: **196/196 passed**. Separate Node receiver suite: **28/28 passed**.
- Docker image `ai-cost-reporter:publication-validation-20261010` built from the exact service context, ID `sha256:a19f8c578f850af9977d7e502032a39ae9736a21a48c3ea9d911a2eacc7b1493`. It discovered 196 tests: **193 passed, three Node-dependent tests skipped**; those three passed on the host.
- With networking disabled, UID 999 ran the default/xAI/both offline plans, imports and scheduled CLI `--help` inside a running PID 1 `sleep infinity` container. Runtime `app.py`/helper source hashes matched the worktree, `/data/reports` stayed empty and no secrets were bundled. Owned test containers were cleaned up.
- All **148 public news source-file hashes** were rechecked unchanged. The latest complete GetNewsAPI result, **505 passed/62 infrastructure-dependent skips**, is historical from the prior phase and was not rerun for publication.
- Independent security review found no blocker. No agent live call, credential inspection/generation, report/scheduled job, new Google operation or deployment occurred. Commit/push receipts and any remote CI result are reported separately after the actual action; no final commit hash is predicted.

Historical local accounting-state validation on 2026-10-10 passed:

- Host Python: **196/196 passed**. Separate Node receiver suite: **28/28 passed**.
- Docker image `ai-cost-reporter:inactive-validation-20261010` built successfully, ID `sha256:f68998fc2a68b34f9c88751c865e01954c7878ae8bc06bded0b448ec4fb7b19e`. It discovered 196 tests: **193 passed, three Node-dependent tests skipped**; those three passed on the host.
- With networking disabled, default, xAI-only and both-provider offline plans passed. The container ran PID 1 `sleep infinity` as UID 999; `python /app/app.py --help` verified the scheduled entry-point parser without executing the billing job. `/data/reports` remained empty; no secret files or optional consent/TEST helpers were bundled.
- The image's `app.py` and billing-helper hashes matched the worktree. Independent GetNewsAPI validation passed **505 tests with 62 infrastructure-dependent skips**; all **148 public source-file hashes** matched. News application/configuration remained unchanged.
- No agent live provider call, credential inspection/generation, report-generation/demo/scheduled job, Google operation, Git staging, commit, push or deployment occurred during that phase. The later user-reported corrected live result appears above; no final reconciliation is claimed.

Historical zero-confirmation validation on 2026-10-10, before the distinct no-recorded-usage classification, passed:

- Host Python: **176/176 passed**. Separate Node receiver suite: **28/28 passed**.
- Docker image `ai-cost-reporter:xai-zero-validation-20261010` built successfully, ID `sha256:fe92a9ebd2fb09348e1688a8438f1e97c0c9f8df4f81dbe810c193c62689382f`. It discovered 176 tests: **173 passed, three Node-dependent tests skipped**; those three passed on the host.
- With networking disabled, bundled default and xAI-only diagnostic offline plans passed. The reporter image used unprivileged UID 999, retained its `sleep infinity` command and left `/data/reports` empty. Its four executable source hashes matched the worktree; no secrets, environment files, tests or optional consent/TEST helpers were bundled.
- Independent pinned, network-disabled GetNewsAPI validation discovered **567 tests: 505 passed, 62 database/tool-dependent skips, no failures**. All **148 public source-file hashes** matched. News application/configuration remained unchanged.
- No reporter/demo job, report generation, agent live provider call, credential inspection/generation, Google operation, Git staging, commit, push or deployment occurred. Unit suites used standard synthetic fixtures only. No live confirmation or publication is claimed.

Historical diagnostic validation on 2026-10-10, before the empty-series confirmation change, passed:

- Host Python: **152/152 passed**. Separate Node receiver suite: **28/28 passed**.
- Docker image `ai-cost-reporter:billing-diagnostics-20261010` built successfully, ID `sha256:b782485fbc53b7901151e40f200bc3dc7c42c05e211af895aec589d169af2a16`. It discovered 152 tests: **149 passed, three Node-dependent tests skipped**; those three passed on the host.
- With networking disabled, the bundled `--diagnostic --provider both` offline plan passed. A separate temporary container ran PID 1 `sleep infinity`; unprivileged UID 999 ran the demo through `docker exec`, producing a valid 2,889-byte PDF and `SYNTHETIC_DEMO` JSON with no marker. Only that owned test container was stopped and removed.
- The four executable source-file hashes in the image matched the worktree. GetNewsAPI was not rerun or modified in this diagnostic phase; older news results below remain historical.
- No agent live provider request, credential inspection/generation, Google operation, Git staging, commit, push or deployment occurred. The original checkout's recorded file fingerprints remained unchanged. No live provider diagnosis or new publication is claimed.

## Published billing-accounting corrections

- Provider numeric JSON decodes directly to Decimal from raw JSON, correcting the pre-existing float-decoding digit loss. Validated aggregation uses exact arithmetic independent of the ambient Decimal context; `usd_text` keeps full fixed-point decimal text in JSON/smoke summaries while PDF monetary display rounds cents only at presentation.
- OpenAI uses the official SDK-style bracketed array parameters. Scoped requests include project grouping and reject missing/null/unrequested result project IDs; unique exact daily UTC buckets and completed pagination are required for the complete month-to-date interval. Terminal `next_page` may be null or omitted only with explicit `has_more=false` and complete coverage.
- xAI uses the documented read-only historical analytics POST with exclusive next-day midnight end, `Etc/GMT`, USD SUM and daily granularity. Every unique description series must contain every requested UTC-midnight day exactly once; explicit `limitReached=false` is required. Empty collections, missing/duplicate/out-of-range days and truncated data fail closed.
- The legacy `scripts/billing_smoke_test.py [--date YYYY-MM-DD] [--live]` path defaults to an offline plan without reading provider environment values or making requests. Explicit legacy `--live` uses both providers through the same accounting code, fixed read-only endpoints, TLS verification and no redirects/environment proxy/netrc settings. Diagnostic mode adds the independent checks described above. Neither mode writes reports or markers or performs uploads, notifications, inference or billing mutations.
- The helper is included in the reporter image at `/app/scripts/billing_smoke_test.py`. A future approved container check uses `python /app/scripts/billing_smoke_test.py --date 2026-10-06 --live` through `docker exec`, with protected environment already configured. Current work does not run it live.
- A successful future check includes `generated_at_utc`, the local snapshot timestamp after both provider reads, along with exact daily/month-to-date totals and scopes. This does not establish provider billing finality; compare against console observations with their own timestamps.
- The user reports successful xAI no-recorded-usage classification and corresponding dashboard zeros, as recorded above. OpenAI live/project-scope accounting and final billing reconciliation remain unresolved. The synthetic Google result does not verify provider monetary accuracy, billing latency or console reconciliation. README documents private environment setup, exact UTC daily/month-to-date comparison and delayed-billing procedure; publication makes no new provider or Google call.

## Historical published billing-phase validation on 2026-10-10

The reviewed billing corrections passed these offline checks before publication at `94507895cf460f3ec215a2f011a61ed5b0ff24e4`. These results precede the current diagnostic changes and do not prove a live billing result.

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
- Future reporter deployment is a separate Dokploy Application using Repository `branislav94/coincourier-api`, Branch `dev`, repository build path `/`, Dockerfile Path `services/ai-cost-reporter/Dockerfile` and Docker Context Path `services/ai-cost-reporter`. Keep startup override unset for image `sleep infinity`, one replica and a dedicated persistent `/data/reports` volume writable by the verified runtime UID/GID (tested UID 999). No incoming server/domain/port is required. Keep AutoDeploy and schedule disabled until separately approved; the schedule alone uses `python /app/app.py` at `0 9 * * *` with per-job `Europe/Belgrade`. Confirm installed timezone/next-run behavior rather than changing shared scheduler/news settings. README has exact protected environment and nonlive post-deployment checks; no existing news deployment or current Google deployment/secret is changed.

## Delivery-verification and publication boundaries

Current work prepares authorized publication of the reviewed reporter after the user's final xAI live result; actual publication receipts are reported separately. Reporter-only commit/non-force publication is authorized; deployment, scheduling, new Google changes, report jobs and agent live calls are excluded. Later live rechecks need separate authorization. The original `api-test` checkout, current branch and pending log/diff files remain untouched; never display their contents, stash, reset, modify or commit them. Private fingerprints may verify preservation.

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

The [full xAI billing schema](https://docs.x.ai/developers/rest-api-reference/management/billing.md) explicitly defines exclusive `endTime` and dense points. A complete day therefore ends at the next midnight, rather than `23:59:59`. Empty `timeSeries` is not documented as fully reconciled zero consumption. Two narrowly validated same-scope empty datasets may establish only the user-authorized `NO_RECORDED_USAGE` state, while explicit zero spending still requires covered numeric data. Invoice previews, prepaid balances and invoice totals do not substitute for the same arbitrary UTC consumption interval. Use minimum team-scoped billing-read access and verify the actual permission names offered by the current console.

For a later separately authorized private read-only recheck, use the existing newly rotated xAI Management key and configured team with `--provider xai` for a complete day. Compare exact daily/month-to-date recorded amounts against the same UTC periods and team scope, not purchases, balances or tax-inclusive invoices. The user's final corrected classification is recorded above; final reconciliation remains unresolved. Capture the observation timestamp and recheck the same old UTC period later: the helper makes fresh API reads and late records may change `NO_RECORDED_USAGE` to `RECORDED_SPENDING`. Already delivered reporter snapshots/markers remain unchanged; deliberate replacement uses separately approved `--force-resend`. No new live/report-generation job or upload is authorized for publication.

## Next verification steps

1. From `services/ai-cost-reporter/`, read `AGENTS.md`, `README.md`, `app.py`, both delivery modules, receiver and manifest. Run:

   ```bash
   python -m unittest discover -s tests -v
   node tests/test_apps_script_receiver.cjs
   python app.py --demo --date 2026-10-06 --output-dir ./reports
   ```

2. Inspect the resulting synthetic PDF and JSON. Keep reports and markers ignored by Git/Docker. `--no-send` queries live providers and must not be used as an offline check. For a separately authorized live preview, use `--no-send --output-dir ./reports/preview`; an existing marked snapshot cannot be overwritten by that mode.
3. From the CoinCourier API repository root, build with `docker build -f services/ai-cost-reporter/Dockerfile -t ai-cost-reporter:local services/ai-cost-reporter`. For current publication, use network-disabled parser/offline-plan checks without a report job. Future deployment leaves image `sleep infinity` as startup and keeps `python /app/app.py` only in the independent application schedule. Keep the schedule disabled until separately approved, with installed per-job `Europe/Belgrade` support and expected next local 09:00 verified.
4. The user-reported TEST result already establishes helper response validation for that invocation. Keep the existing script editor, secret and target folder private; no new receiver deployment or setup is part of publication. Future manual Google changes require explanation and separate authorization. No Desktop OAuth JSON is used.
5. The owner must independently inspect the reported TEST PDF/JSON and confirm no duplicates, then manually disable/remove `AI_COST_TEST_UPLOADS_ENABLED`. These actions remain unconfirmed. Later separately authorized TEST checks use the documented guarded helper, dedicated namespace and `replace=false`. Node doubles cover partial failures and other protocol cases but do not establish Google concurrency or quotas.
6. The user-reported corrected xAI live result is recorded above; no new provider call is needed for publication. Later separately authorized rechecks may use `scripts/billing_smoke_test.py --date 2026-10-06 --diagnostic --live --provider xai` privately with the existing newly rotated Management key and team (the date is an example). The agent must not inspect credential values or generate/retrieve keys. Compare exact recorded amounts and matching UTC scope with the console, retaining absence/finality caveats and unknown errors. OpenAI live/project-scope verification remains pending.
7. Publish only the reviewed reporter changes under the user's current commit/non-force push authorization. Do not deploy Dokploy, enable a production schedule, change Google deployment/properties, alter GetNewsAPI or make agent provider requests. Record actual publication/CI receipts separately; preserve original pending files and never predict the final commit hash.

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
- Before production, obtain explicit deployment and single daily timezone-correct schedule authorization; verify partial-upload retries do not duplicate files.

Repository privacy, remote CI status, Google account consent, real API behavior and production deployment are external state. Verify them when authorized; do not infer completion from this handoff.
