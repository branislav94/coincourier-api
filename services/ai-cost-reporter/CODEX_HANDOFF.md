# Codex handoff — AI Cost Reporter

## Current architecture

This handoff supersedes the original OAuth-first setup instructions. The daily Dokploy Python job retrieves actual OpenAI organization and xAI team billing costs for a completed UTC day, generates PDF + JSON snapshots and securely POSTs them to a CoinCourier-owned Google Apps Script web app. The receiver creates `CoinCourier My Drive / AI Infrastructure Costs / YYYY / MM`. Schedule once daily at `09:00 Europe/Belgrade`, with one replica and persistent `/data/reports` storage.

The service lives at `coincourier-api/services/ai-cost-reporter/` in `github.com/branislav94/coincourier-api`, with its own image, requirements, entry point, protected environment and report volume. The original standalone source project remains intact. The operator-maintained `.env.example` was copied byte-for-byte and remains OAuth-oriented; it does not enumerate every Apps Script setting. Configure `GOOGLE_DRIVE_BACKEND=apps_script`, `GOOGLE_APPS_SCRIPT_WEB_APP_URL` and `GOOGLE_APPS_SCRIPT_SHARED_SECRET` explicitly in protected reporter settings. To preserve existing installations, the code defaults to `oauth` when the backend variable is unset. The legacy OAuth uploader and Windows helper remain available separately; their original month-folder convention is `YYYY-MM`.

## Migration boundaries and paths

- No credentials, `.env`, virtual environments, generated reports/markers or Git metadata were copied. The retained `.env.example` is a template only. Do not copy or expose the source project's local/private artifacts.
- Run all reporter Python/Node test and local demo commands from `services/ai-cost-reporter/`, using this service's requirements in a separate Python environment. The existing news entry point is `GetNewsAPI/app.py`.
- Reporter CI is `.github/workflows/ai-cost-reporter-offline.yml` at the repository root, with service working directory and service-specific requirements cache input. The standalone GitHub publication script is omitted because it would initialize/publish a nested repository. The legacy OAuth consent helper is retained.
- GetNewsAPI functions, databases, dependencies, migrations, Compose files and production configuration remain unchanged. The existing root Dockerfile copies only `GetNewsAPI/` and specific `maintenance/` paths, so no root Dockerfile or `.dockerignore` change is needed.
- Future reporter deployment is a separate Dokploy Application using repository build path `/`, Dockerfile Path `services/ai-cost-reporter/Dockerfile` and Docker Context Path `services/ai-cost-reporter`. No existing news deployment is changed. Keep one reporter replica, a separate persistent `/data/reports` volume, no incoming port and scheduled command `python /app/app.py`.

## Authorized publication and cleanup

The current task authorizes a commit named `Add standalone AI infrastructure cost reporter` and non-force publication to the verified CoinCourier `origin/dev`. If branch protection blocks that push, use a feature branch and pull request targeting `dev`. Do not force-push, change the default branch or deploy.

Prepare the change in `C:\Users\Win11\Documents\GitHub\api-test-ai-cost-reporter-dev-20261009`, based on `origin/dev` at `abe628b02411b1894e698d2bb0b4db82db5f38e8`. The original `api-test` checkout, current branch and pending log/diff files stay untouched: never display their contents; never stash, reset, modify or commit them. Compare fingerprints privately if needed to verify preservation. Stage only this service and `.github/workflows/ai-cost-reporter-offline.yml` after offline checks and a safe staged-source scan.

The service was copied from `gamingnewsapi/services/ai-cost-reporter/`. Only after successful CoinCourier publication, clean up the 21 recorded migration paths in that checkout and reverse only its reporter-specific root ignore addition. Keep unrelated changes intact and verify all resolved cleanup targets remain within the intended checkout. The original standalone reporter remains retained. Publication does not authorize deployment, account consent or live provider/Drive calls.

## CoinCourier verification on 2026-10-09

- The latest 19 reporter source/configuration/documentation/test files were copied from the reviewed GamingNewsAPI service, along with its dedicated root CI workflow. All Python ASTs match the source; only inherited trailing whitespace and migration documentation changed. Apps Script files, Dockerfile and dependency manifests remain unchanged.
- Reporter validation passed in the isolated CoinCourier worktree: 45 Python tests, 20 actual receiver scenarios with Google services mocked, import-origin checks and synthetic PDF/JSON generation.
- Reporter Docker build passed with `services/ai-cost-reporter` as context. The network-disabled container passed 43 tests and skipped two Node-dependent tests already passed on the host. Its unchanged running-container command accepted the demo through `docker exec`; output was generated as a non-root user, with no GetNewsAPI, credentials or Git files in the image.
- Independent GetNewsAPI regression validation used its exact pinned requirements and a read-only public source snapshot in a network-disabled Python 3.11 container. It passed 505 tests; 62 MariaDB/Compose infrastructure checks were skipped. Existing dotenv isolation was active, and no databases or live provider endpoints were contacted.
- Existing GetNewsAPI source, runtime dependencies, root Dockerfile, root ignore rules, Compose files and deployment configuration remain untouched. Only the new service and its new CI workflow are staged for this migration.
- Publication and guarded cleanup receipts are reported separately after completion; no second documentation commit is required. Remote GitHub CI and live billing/Drive integrations remain unverified. Validation scratch files and test images remain outside the published service tree.

## Implemented delivery components

- `apps_script_delivery.py`: bounded PDF/JSON artifacts; exact UTF-8 HMAC-SHA256 protocol; body signature; fresh timestamp/nonce per attempt; restricted HTTPS `/exec` endpoint and ContentService response redirect; streamed bounded response and strict success validation.
- `google_apps_script/Code.gs`: authenticated `doPost(e)`; ±300-second timestamp window; durable nonce storage with expiry and capacity bounds; script lock; complete payload/date/name/MIME/hash checks before Drive access; private folder hierarchy; identical-file reuse; partial-pair recovery; explicit conflicts; file-ID-preserving force replacement through Advanced Drive v3. `doGet` never accesses Drive or discloses stored data.
- `google_apps_script/appsscript.json`: V8 runtime, Advanced Drive v3 service and full Drive owner scope. Manual setup requires CoinCourier owner consent, execute-as-owner deployment and anonymous callers.
- `app.py`: selectable delivery backend, destination-aware markers, preserved report snapshots after partial failures, and durable force-replacement intent.
- Offline Python tests and the Node `Code.gs` harness cover signing, success/rejection bodies, malformed responses, replay, integrity, duplicates, partial delivery and replacement. Google service doubles make no live calls.
- `README.md` provides exact Script Properties, manifest, deployment, Dokploy, protocol and retry instructions. No earlier Google OAuth secrets are needed for Apps Script.

The shared secret is newly generated privately: exactly 64 lowercase hex characters, copied into Apps Script `AI_COST_SHARED_SECRET` and protected Dokploy `GOOGLE_APPS_SCRIPT_SHARED_SECRET`. The key is literal UTF-8 text, not decoded hexadecimal bytes. Do not request, read, log or commit it. README's manual Windows generator copies directly to a clipboard with history/sync disabled; it prints or saves nothing, and the clipboard is cleared after setup.

## Local verification completed on 2026-10-07

These results belong to the original standalone source project before migration.

- Baseline: 19 existing tests passed, plus the synthetic PDF/JSON demo.
- Final: 45 Python tests passed on Windows Python 3.13, including Python-signed requests executed through the actual receiver with Google services mocked. All 20 Node receiver scenarios passed.
- The final Docker image built successfully on Python 3.12. With networking disabled, 43 Python tests passed and two Node-dependent tests were skipped (already passed on the host). The default running container accepted the Dokploy-style `docker exec` demo command and generated PDF/JSON as the unprivileged reporter user.
- Billing fetchers, report schema/aggregation and PDF definitions were compared against the original Git version and remained unchanged. Source-only secret-pattern scans and `git diff --check` passed; no files were staged, committed or pushed.
- No real credentials were read/generated, no provider/Google account API calls were made, and no production deployment or scheduling was performed. The local `.venv` and synthetic reports are ignored artifacts.

## Billing implementation and open compatibility check

Existing OpenAI pagination/line-item aggregation, xAI USD analytics/truncation checks, daily and month-to-date reporting and explicit scope remain intact. Both provider responses must succeed. UTC report days are independent of the job's Serbian execution timezone; billing may be delayed or adjusted.

Official OpenAI REST/Python docs confirm `project_ids` is an array. Offline inspection of installed official OpenAI Python SDK **1.70.0** showed bracketed query names (`project_ids[]=...` and `group_by[]=line_item`); the existing `requests` path emits repeated unbracketed names. Server acceptance of that encoding and live project-scoped accuracy remain unverified. Treat this as a separate compatibility check; do not claim live scoped totals are correct from offline mocks.

## Next verification steps

1. From `services/ai-cost-reporter/`, read `AGENTS.md`, `README.md`, `app.py`, both delivery modules, receiver and manifest. Run:

   ```bash
   python -m unittest discover -s tests -v
   node tests/test_apps_script_receiver.cjs
   python app.py --demo --date 2026-10-06 --output-dir ./reports
   ```

2. Inspect the resulting synthetic PDF and JSON. Keep reports and markers ignored by Git/Docker. `--no-send` queries live providers and must not be used as an offline check. For a separately authorized live preview, use `--no-send --output-dir ./reports/preview`; an existing marked snapshot cannot be overwritten by that mode.
3. From the CoinCourier API repository root, build with `docker build -f services/ai-cost-reporter/Dockerfile -t ai-cost-reporter:local services/ai-cost-reporter`. Run its offline container demo with networking disabled; preserve the unchanged `python /app/app.py` scheduled command in its independent application. Verify Dokploy's installed scheduler supports `Europe/Belgrade` and displays the expected next local 09:00 execution before enabling its schedule.
4. After separate user authorization, perform manual Apps Script setup using README. Keep the script editor and target folder private. Enable Drive API manually only if using a standard Cloud project; the default project enables it with the Advanced Drive service. No Desktop OAuth JSON is used.
5. After separate authorization, validate the real Google runtime in a private test destination: owner consent, signed upload, response redirect/body, fresh-auth duplicate retry, partial-pair recovery, invalid/expired/replayed rejection and force replacement preserving IDs. Node doubles cannot establish actual service permissions, quotas, concurrency or Google runtime behavior.
6. Before production billing smoke tests, revoke and replace previously disclosed OpenAI/xAI keys. Use only authorized, newly rotated credentials. Verify BOTH billing providers and intended scope/UTC periods; log only safe status/amount summaries. Do not assert live compatibility from documentation alone.
7. Complete the explicitly authorized CoinCourier `origin/dev` commit/publication and its guarded post-success cleanup as described above. Production provisioning, deployment, scheduling, account consent and live smoke tests still require separate authorization.

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
- CoinCourier's manual Apps Script consent/deployment succeeds, and a separately authorized runtime check verifies private report delivery and idempotent retry.
- Authorized provider smoke tests validate both APIs, project-filter encoding, team scope and complete UTC periods.
- Production deployment and one daily timezone-correct schedule are explicitly authorized; reruns complete partial uploads without duplicate files.

Repository privacy, remote CI status, Google account consent, real API behavior and production deployment are external state. Verify them when authorized; do not infer completion from this handoff.
