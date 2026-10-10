# CoinCourier — Daily AI Cost Reporter

A small Python 3.12 Docker/Dokploy daily job retrieves **actual provider-recorded OpenAI and xAI API costs**, aggregates amounts with `Decimal`, generates PDF and JSON snapshots, and delivers both to CoinCourier Google Drive through an authenticated Google Apps Script web app. Email and WhatsApp remain optional.

## Location and service isolation

This service lives at **`coincourier-api/services/ai-cost-reporter/`** in the [CoinCourier API repository](https://github.com/branislav94/coincourier-api), independently of the existing GetNewsAPI application. It has its own Dockerfile, requirements, entry point, offline tests, environment settings and persistent report volume. The original standalone `ai-cost-reporter` project is retained intact. No credentials, `.env`, virtual environments, generated reports, delivery markers or Git metadata were copied. The public `.env.example` now documents Apps Script delivery and the separately optional legacy channels.

Reporter commands in this document run from the **service directory**, unless explicitly labeled as repository-root or container commands. From the CoinCourier API repository root:

```bash
cd services/ai-cost-reporter
```

The normal local repository is `C:\Users\Win11\Documents\GitHub\api-test`. This change is prepared in a separate `origin/dev`-based worktree, so its Windows service directory is:

```powershell
Set-Location 'C:\Users\Win11\Documents\GitHub\api-test-ai-cost-reporter-dev-20261009\services\ai-cost-reporter'
```

Keep the GetNewsAPI and reporter Python environments separate. Install reporter requirements only into the reporter environment. The existing news entry point is `GetNewsAPI/app.py`; this service's entry point is `services/ai-cost-reporter/app.py`. Run reporter commands from this service directory.

GetNewsAPI code, functions, databases, dependencies, Compose files and production configuration remain unchanged. Its existing root Dockerfile copies only `GetNewsAPI/` and specific `maintenance/` directories, so the reporter cannot enter that image. No root Dockerfile or `.dockerignore` edit is required. Reporter CI is a separate root workflow at `.github/workflows/ai-cost-reporter-offline.yml`, with its commands working in this service directory and dependency caching keyed to `services/ai-cost-reporter/requirements.txt`. GitHub requires workflows at the repository-root [`.github/workflows` location](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax); a nested service workflow is not used.

The old `scripts/publish_to_github.ps1` helper is intentionally omitted. It initializes and publishes a standalone repository, which could create nested Git metadata inside the monorepo. The separately authorized legacy OAuth helper remains available. The user now authorizes publication of the reviewed reporter changes with commit message `Complete AI Cost Reporter Google Drive integration` and a non-force push to `origin/dev`. Dokploy deployment, production scheduling, billing requests and further Google changes remain separately authorized actions. The original `api-test` checkout and its pending files remain untouched.

```text
Dokploy Application Schedule Job: 09:00 Europe/Belgrade
    -> Python: previous complete UTC day
    -> OpenAI Organization Costs + xAI Management Billing
    -> PDF + JSON snapshots in persistent /data/reports
    -> HTTPS POST with body HMAC to Apps Script /exec
    -> CoinCourier My Drive / AI Infrastructure Costs / YYYY / MM
       ai-cost-report-YYYY-MM-DD.pdf
       ai-cost-report-YYYY-MM-DD.json
```

Costs describe provider-recorded API consumption. They exclude tax, credit purchases, consumer ChatGPT/Grok subscriptions and hosting costs. Billing records can arrive late or change; a report is a dated snapshot. Both billing APIs must succeed, and failures result in a nonzero exit rather than a fabricated $0 amount.

## Credentials and billing scope

- Use a newly rotated OpenAI **organization Admin API key** and xAI **Management key**, with the intended xAI team ID. Previously disclosed keys must be revoked and replaced before deployment.
- `OPENAI_PROJECT_IDS` blank means the OpenAI organization; a comma-separated list requests only those projects. xAI always covers the configured team. Read the project-filter compatibility note below before relying on scoped totals.
- Never paste credentials into chat, source, logs, reports, Git or build arguments. Keep report output and delivery markers on protected persistent storage. `.env`, `.secrets/`, credential files and local reports are Git-ignored and Docker-ignored.
- Apps Script delivery uses a random shared secret in **two permanent stores only**: Apps Script Script Properties and protected Dokploy environment variables. The existing live TEST deployment already has a configured secret; this publication does not generate, disclose or rotate it. It requires no Desktop OAuth client JSON or Python OAuth tokens. Leave `GOOGLE_OAUTH_*` empty for this backend; earlier OAuth secrets are not used.

## One-time Google Apps Script setup

These are manual setup instructions for a new installation. The existing TEST deployment has already returned the user-reported authenticated responses documented below; do not recreate its project, replace its secret or redeploy it merely to publish source changes. No deployment, cloud authorization or production upload is performed by the offline tests.

1. Sign in to [Google Apps Script](https://script.google.com/) using the **CoinCourier Google account that owns the target My Drive**. Create a new standalone project, for example `CoinCourier AI Cost Receiver`. Keep editing access limited to trusted administrators; editors can read Script Properties.
2. Replace the editor's `Code.gs` contents with [google_apps_script/Code.gs](google_apps_script/Code.gs). Open **Project Settings**, enable **Show "appsscript.json" manifest file in editor**, return to the editor, and replace its manifest with [google_apps_script/appsscript.json](google_apps_script/appsscript.json). Save both files.
3. Confirm **Services → Drive API**, version **v3**, identifier **Drive**, is enabled. The supplied manifest enables this Advanced Drive service. It is required by `authorizeSetup` and by `Drive.Files.update` when replacing existing PDF/JSON files without changing their IDs. With the automatically created default Cloud project, Google enables the API automatically. If you attach a standard Google Cloud project, manually enable **Google Drive API** in that project's Cloud Console. See [Google's advanced-service instructions](https://developers.google.com/apps-script/guides/services/advanced).
4. For a future new installation, privately establish a cryptographically random **32-byte value encoded as exactly 64 lowercase hexadecimal characters**, without printing it or saving it outside the two permanent stores. Do not use a password, example test values or an earlier OAuth secret. Preserve the existing configured value for the current installation; no generation or rotation is needed for publication. The code uses the literal 64-character text as the HMAC key, without hex-decoding it.
5. Open **Project Settings → Script Properties → Add script property**, then save the following properties. Google documents this interface in [Properties Service](https://developers.google.com/apps-script/guides/properties).

   | Property | Value |
   | --- | --- |
   | `AI_COST_SHARED_SECRET` | The private 64-character lowercase hex secret. |
   | `AI_COST_PARENT_FOLDER_ID` | Optional: ID of a private parent folder owned by CoinCourier in My Drive. Leave absent/empty to use My Drive root. |

   The receiver creates `AI Infrastructure Costs / YYYY / MM` underneath that parent automatically. Supply the parent folder ID alone, without its URL. Keep the parent and existing child hierarchy private: Drive files inherit parent permissions. Do not point this receiver at a Shared Drive or a publicly shared folder. If duplicate same-name folders already exist, the receiver fails so an administrator can resolve the ambiguity.
6. In the editor's function selector choose **authorizeSetup**, then **Run**. Review and grant the requested Drive permission as CoinCourier. This function checks configuration and access without creating reports. Apps Script manages its own owner authorization; its manifest requests `https://www.googleapis.com/auth/drive`, which is broader than the legacy backend's `drive.file`. Review that permission accordingly. [DriveApp authorization documentation](https://developers.google.com/apps-script/reference/drive/drive-app) specifies the scope needed for folder/file creation.
7. When ready to deploy, select **Deploy → New deployment → Web app**. Choose **Execute as: Me (CoinCourier)** and **Who has access: Anyone**, including callers who are not signed in. Confirm the deployment. The public endpoint authenticates uploads with the body HMAC; it does not make Drive files public. If organizational policy does not permit anonymous web-app access, this backend cannot be used with these settings. See [Google's web-app deployment and execution-identity guide](https://developers.google.com/apps-script/guides/web).
8. Copy the deployed HTTPS URL ending in **`/exec`** into Dokploy's `GOOGLE_APPS_SCRIPT_WEB_APP_URL`. Use the original `https://script.google.com/macros/s/.../exec` URL without query parameters. `/dev` is restricted to script editors and is unsuitable for the daily job.
9. Set Dokploy's protected `GOOGLE_APPS_SCRIPT_SHARED_SECRET` to the **same literal secret** as `AI_COST_SHARED_SECRET`. Do not put the secret in the URL, request headers, source code or a checked-in `.env` file. Do not copy the configured value into chat or publication commands.

For an independently authorized future receiver change, save it and use **Deploy → Manage deployments → Edit → New version → Deploy** for the existing deployment. Its URL remains stable. A saved editor change alone does not update a versioned web app; see [Google's deployment guide](https://developers.google.com/apps-script/concepts/deployments). Any future secret rotation must be separately authorized and coordinated between both private stores. Keep the Dokploy host clock synchronized.

`doGet` returns only `{"ok":false,"error":"method_not_allowed"}`. It never accesses Drive or returns reports, names, folder IDs, configuration or credentials.

## Verify delivery with an isolated Google TEST upload

**User-reported live result, recorded on 2026-10-10:** status `api_confirmed`, with three authenticated successes and one rejected invalid-HMAC request. Retry responses preserved the same two file IDs and hashes. No billing calls were made, and temporary local artifacts were deleted. The owner still needs to independently inspect [the reported TEST folder](https://drive.google.com/drive/folders/131uquIdcNrqVymYjPsVJYhxAI61eM14l) and confirm exactly `ai-cost-report-TEST-2026-10-06.pdf` and `ai-cost-report-TEST-2026-10-06.json`, their TEST content and absence of duplicate files. This documentation records the user's helper result; it does not claim an agent independently opened Drive. After inspection, the owner should manually set `AI_COST_TEST_UPLOADS_ENABLED=false` or remove it. That change is not confirmed. The execution date, nonce values and hash values were not supplied.

This verification checks Google delivery without querying OpenAI/xAI or labeling synthetic amounts as provider-recorded spend. The helper [scripts/test_apps_script_upload.py](scripts/test_apps_script_upload.py) uses JSON `data_type=SYNTHETIC_DELIVERY_TEST`, a `test_namespace` beginning with `test-`, and visibly marked TEST PDF/JSON filenames:

```text
ai-cost-report-TEST-YYYY-MM-DD.pdf
ai-cost-report-TEST-YYYY-MM-DD.json
```

It retains the protocol-v1 HMAC algorithm. The exact TEST filenames are signed, and the raw JSON SHA-256 binds both its synthetic data type and namespace to the signature. Test uploads require `replace=false`; they cannot replace production reports. The receiver stores them only at:

```text
<dedicated private test parent> / AI Cost Reporter Tests / <test_namespace> / YYYY / MM
```

**Prerequisites for a new or repeated live check:** the deployed version must contain the guarded TEST receiver code. The reported successful check used this TEST path; these instructions are not a request to redeploy the existing receiver. Review these steps before any separately authorized Google change. Local source changes do not update the deployed web app.

1. As CoinCourier, create or choose a **private dedicated test parent folder in My Drive**. Use a folder separate from the actual-report parent, with no public sharing. Supply its ID alone.
2. Review the updated `google_apps_script/Code.gs`, then manually replace the existing project's receiver code with it. Keep the existing shared secret private. No Desktop OAuth JSON or billing credentials are involved.
3. In **Project Settings → Script Properties**, add `AI_COST_TEST_UPLOADS_ENABLED=true` and `AI_COST_TEST_PARENT_FOLDER_ID=<private test parent ID>`. These are Google Script Properties, not Python/Dokploy environment variables. Both are required; missing/disabled TEST configuration rejects the upload before Drive operations and never falls back to the production parent. Google's [Properties Service instructions](https://developers.google.com/apps-script/guides/properties) describe this interface.
4. Retain the supplied V8 manifest, full Drive owner scope and Advanced Drive v3 service. No additional scope is needed for this TEST path. If using a standard Cloud project, ensure Drive API is enabled; the default Apps Script Cloud project enables it with the advanced service. Review [Google's advanced-service instructions](https://developers.google.com/apps-script/guides/services/advanced).
5. Use **Deploy → Manage deployments → Edit → New version → Deploy** to update the existing `/exec` endpoint, keeping **Execute as: Me** and access for **Anyone**, including callers who are not signed in. Keep the existing endpoint URL. [Google's deployment guidance](https://developers.google.com/apps-script/concepts/deployments) explains version updates; [web-app documentation](https://developers.google.com/apps-script/guides/web) explains execution identity and `/dev` restrictions.
6. After verification, set `AI_COST_TEST_UPLOADS_ENABLED=false` or remove it. Existing TEST artifacts remain in the isolated folder for inspection. Removing them is a separate manual action; the helper does not delete Drive files.

From the service directory, first run the offline helper in the reporter Python environment:

```powershell
python .\scripts\test_apps_script_upload.py --date 2026-10-06 --namespace test-delivery-check
```

Without `--live`, it creates deterministic synthetic artifacts in temporary storage, validates them, and prints only the namespace, date, filenames, sizes, hashes and intended test path. It makes no network request and deletes the temporary files. No `--output-dir` is needed. Namespaces must match `test-[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?`. Omitting `--namespace` creates and prints a new namespace; reuse that namespace and date when repeating a test. The same inputs regenerate identical artifact bytes, allowing duplicate checks without timestamp-content conflicts.

For a live check **only after the owner completes the Google steps**, provide the shared secret privately through the named process environment variable. This PowerShell command prompts without displaying it, uses no clipboard or `.env` file, and clears the temporary environment value and native secret buffer afterward. Replace the URL placeholder with the existing deployed `/exec` URL; do not put a secret in that URL or command arguments.

```powershell
$aiCostSecretInput = Read-Host 'Apps Script shared secret (not displayed)' -AsSecureString
$aiCostSecretPointer = [IntPtr]::Zero
try {
    $aiCostSecretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($aiCostSecretInput)
    $env:GOOGLE_APPS_SCRIPT_SHARED_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($aiCostSecretPointer)
    python .\scripts\test_apps_script_upload.py --live `
        --url 'https://script.google.com/macros/s/DEPLOYMENT_ID/exec' `
        --date 2026-10-06 --namespace test-delivery-check
    if ($LASTEXITCODE -ne 0) { throw 'TEST delivery verification failed; inspect the sanitized helper result.' }
} finally {
    Remove-Item -LiteralPath Env:\GOOGLE_APPS_SCRIPT_SHARED_SECRET -ErrorAction SilentlyContinue
    if ($aiCostSecretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($aiCostSecretPointer)
    }
    $aiCostSecretInput.Dispose()
    Remove-Variable aiCostSecretInput, aiCostSecretPointer
}
```

The live helper uploads the pair, retries with fresh authentication and requires the same two file IDs, sends an invalid HMAC and requires rejection, then retries validly and verifies the IDs remain unchanged. It validates bounded response JSON, including `mode=test`, matching `test_namespace`, a valid `folder_id`, and both matching file identities/hashes. It follows the permitted [ContentService response redirect](https://developers.google.com/apps-script/guides/content); HTTP 200 alone is insufficient. A successful helper check plus owner inspection of the two TEST files establishes delivery for that endpoint and test parent, not provider billing accuracy or Dokploy execution. No provider keys are required by the helper, and it does not import the daily reporter or read/load `.env`.

For any later invocation, an outdated receiver, absent TEST properties or an unavailable private process secret prevents a live check. The current user-reported check is `api_confirmed`; independent owner inspection remains pending. Do not substitute `PROVIDER_REPORTED_API_SPEND` for synthetic data or use ordinary `--demo` files as production uploads.

## Dokploy configuration

After deployment is separately authorized, configure a **separate Dokploy Application for this reporter** using the CoinCourier API repository. Retain existing GetNewsAPI deployment settings and services. Configure the reporter build fields as follows:

| Reporter build setting | Value |
| --- | --- |
| Repository | `branislav94/coincourier-api` |
| Branch | `dev` |
| Build type | `Dockerfile` |
| Repository build path | `/` |
| Dockerfile Path | `services/ai-cost-reporter/Dockerfile` |
| Docker Context Path | `services/ai-cost-reporter` |
| Docker Build Stage | Empty |

Check the installed Dokploy version's resulting build command uses these paths; [Dokploy documents separate Dockerfile and context fields](https://docs.dokploy.com/docs/core/applications/build-type). The context must be the service directory so its own `.dockerignore` and requirements apply. Use an independent reporter image and volume, with no news database connection, incoming server, public domain or exposed port. The container command remains `python /app/app.py` for scheduled execution.

The following values contain no secrets:

```dotenv
GOOGLE_DRIVE_ENABLED=true
GOOGLE_DRIVE_BACKEND=apps_script
OUTPUT_DIR=/data/reports
EMAIL_TO=
WHATSAPP_TO=
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GOOGLE_OAUTH_REFRESH_TOKEN=
```

Enter `GOOGLE_APPS_SCRIPT_WEB_APP_URL`, `GOOGLE_APPS_SCRIPT_SHARED_SECRET`, newly rotated `OPENAI_ADMIN_KEY`, `XAI_MANAGEMENT_KEY`, `XAI_TEAM_ID` and optional `OPENAI_PROJECT_IDS` through this reporter application's protected Dokploy environment configuration. **Set `GOOGLE_DRIVE_BACKEND=apps_script` explicitly.** The public `.env.example` lists the complete delivery configuration; it contains no secrets and now selects Apps Script as its example backend. Python does not automatically read/load `.env`, so values must actually enter the container environment. `GOOGLE_DRIVE_FOLDER_NAME` configures the legacy OAuth backend only; the Apps Script folder name is fixed. TEST enablement and its parent ID belong only in Google Script Properties, not this container's environment.

| Variable group | Requirement |
| --- | --- |
| `GOOGLE_DRIVE_ENABLED`, `GOOGLE_DRIVE_BACKEND` | `true` and `apps_script` for Apps Script delivery. |
| `GOOGLE_APPS_SCRIPT_WEB_APP_URL`, `GOOGLE_APPS_SCRIPT_SHARED_SECRET` | Required for Apps Script; deployed `/exec` URL and matching 64-character lowercase hex secret. |
| `OPENAI_ADMIN_KEY`, `XAI_MANAGEMENT_KEY`, `XAI_TEAM_ID` | Required by the real daily billing job, using newly rotated keys; unnecessary for the synthetic helper. |
| `OPENAI_PROJECT_IDS` | Optional comma-separated project filter; blank requests organization-wide OpenAI costs. |
| `OUTPUT_DIR` | Defaults to `/data/reports`; keep its mount persistent. |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` | Required only when explicitly choosing legacy `oauth`; leave blank for Apps Script. |
| `GOOGLE_DRIVE_FOLDER_NAME` | Legacy-only folder name; defaults to `AI Infrastructure Costs`. |
| `EMAIL_TO` | Blank/absent disables email. If enabled, require `SMTP_HOST` and `SMTP_FROM`; `SMTP_SECURITY` is `starttls` or `ssl`, with default port 587 or 465. `SMTP_USERNAME`/`SMTP_PASSWORD` must be both set or both blank. |
| `WHATSAPP_TO` | Blank/absent disables WhatsApp. If enabled, require `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID`; optional API/template/language defaults appear in the template. |

Mount persistent storage at **`/data/reports`** and run **one replica**. Retain both reports and `.delivery.json` markers across restarts. If this later replaces an existing reporter deployment, retain its report volume and keep only one reporter schedule active during the cutover; do not copy reports or markers into Git or the Docker build context. The reporter volume stays separate from GetNewsAPI storage. The container's `sleep infinity` process keeps it available for scheduled commands; Python needs no incoming server, public domain or open port.

Configure one Application Schedule Job:

| Setting | Value |
| --- | --- |
| Cron expression | `0 9 * * *` |
| Timezone | `Europe/Belgrade` |
| Command | `python /app/app.py` |

The job runs at Serbian local 09:00, while each report covers a completed **UTC billing day**. Check the installed Dokploy scheduler's timezone support and the next displayed execution time before enabling the job. [Dokploy's schedule-job guide](https://docs.dokploy.com/docs/core/schedule-jobs) describes running commands inside application containers.

## Authentication protocol and bounded reports

Python sends an `application/json` envelope with exactly these fields. Production reports and the guarded TEST extension both retain version 1:

```text
version: 1
timestamp: canonical decimal Unix-seconds string
nonce: 32 lowercase hex characters, freshly random per attempt
report_date: YYYY-MM-DD, completed UTC day from 2000 onward
replace: boolean
files: [PDF descriptor, JSON descriptor]
signature: lowercase hex HMAC-SHA256
```

Each descriptor contains `name`, `mime_type`, integer raw-byte `size`, lowercase hex `sha256`, and standard padded `content_base64`. The exact canonical signing text is these **13 lines**, joined with LF (`\n`), with **no trailing newline**:

```text
AI-COST-REPORTER-V1
<timestamp>
<nonce>
<report_date>
<replace: 1 for true, 0 for false>
ai-cost-report-<report_date>.pdf
application/pdf
<PDF size in decimal>
<PDF SHA-256>
ai-cost-report-<report_date>.json
application/json
<JSON size in decimal>
<JSON SHA-256>
```

The signature is `hex_lower(HMAC-SHA256(UTF8(literal_secret), UTF8(canonical_text)))`. SHA-256 hashes cover **raw decoded file bytes**. Neither JSON envelope formatting nor Base64 text is directly signed; their contents must match the signed byte lengths and digests. [Apps Script Utilities](https://developers.google.com/apps-script/reference/utilities/utilities) supplies the corresponding UTF-8 HMAC and byte-digest functions.

For the TEST extension, only the two filename lines change to `ai-cost-report-TEST-<report_date>.pdf` and `.json`; `replace` must be false. `SYNTHETIC_DELIVERY_TEST` and `test_namespace` are inside the hashed JSON bytes, so changing either invalidates payload integrity and its signature. The TEST response also confirms the test mode, namespace and folder ID. Ordinary `SYNTHETIC_DEMO` outputs remain rejected by both publisher and receiver.

The receiver accepts a timestamp only within **±300 seconds** of its current clock. Under a [script-wide lock](https://developers.google.com/apps-script/reference/lock/lock-service), it prunes expired nonce properties and records each authenticated nonce **before** accessing Drive. A nonce remains recorded through `timestamp + 300 seconds`, including after a storage failure. Exact replay is rejected; a legitimate retry uses a new timestamp, nonce and signature. At most 512 live nonce properties are retained, and capacity exhaustion fails explicitly. Properties Service is durable for this purpose; the ledger is kept bounded because Google [limits property storage and service quotas](https://developers.google.com/apps-script/guides/services/quotas).

The receiver accepts exactly one PDF and one JSON for the declared date, in that order. It verifies exact names/MIME types, Base64 encoding, byte lengths and hashes, PDF header/end marker, and matching provider-report JSON identity. Limits are **2 MiB PDF**, **512 KiB JSON**, **4 MiB request** and **64 KiB response**. Authentication and payload checks precede all Drive operations.

Apps Script's [ContentService](https://developers.google.com/apps-script/guides/content) redirects its output to `script.googleusercontent.com`. Python follows the permitted Google response redirect as a GET, retains TLS verification, and checks the bounded response body. HTTP 200 alone is insufficient: success requires `ok: true`, protocol version/date, boolean duplicate state and both valid file IDs with matching filenames and hashes. `ok: false`, malformed JSON, an unsafe redirect, mismatched content, transport errors and storage failures all fail delivery without a success marker. Logs contain neither request bodies nor server exception text.

## Retry, duplicate and replacement behavior

The reporter saves a delivery marker before its first upload, then reuses the **same PDF and JSON snapshot** on unfinished reruns. It does not refetch billing or regenerate the PDF merely because a response was lost. Run the same dated command again after a failure; each invocation signs a fresh request.

The receiver holds its lock while finding/creating folders and files. It checks both filenames before writing either report. Same-name files with identical bytes are reused; if only one file was created before a failure, a retry reuses it and creates the missing counterpart. Multiple matches or different bytes under the same filename fail explicitly. These checks provide idempotent retries; the two Drive file writes are separate operations, so a partially complete pair can exist until a retry succeeds.

To intentionally refetch adjusted billing and replace a prior report, use:

```bash
python /app/app.py --date 2026-10-06 --force-resend
```

`--force-resend` regenerates the snapshot and sets the signed `replace` flag. Advanced Drive `Drive.Files.update` replaces differing report bytes while preserving file IDs. The local replacement intent survives a partial failure for the same backend/destination, so a subsequent ordinary retry can finish that same replacement. Existing ambiguous names or wrong MIME types still fail. Force resend can also resend enabled email/WhatsApp notifications.

Success markers distinguish the delivery backend and a hash of its destination; changing the backend or `/exec` URL triggers delivery to that destination. Changing only the receiver's parent-folder property behind the same URL is not visible to the local marker: use a new deployment URL or intentionally use `--force-resend` if an existing day must be delivered there. Markers are local delivery evidence, not ongoing reconciliation of remote deletions or external edits.

## Offline verification and later live checks

From `services/ai-cost-reporter/`, using a separate reporter Python 3.12+ environment and Node.js:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
node tests/test_apps_script_receiver.cjs
python app.py --demo --date 2026-10-06 --output-dir ./reports
```

On this Windows machine `py -3.13` can be substituted for `python`. `--demo` generates synthetic reports and never calls providers or delivers. `--no-send` disables delivery but **still queries both live billing APIs**; it is not an offline test option.

For a live billing preview of an already delivered date, use a separate output directory:

```bash
python app.py --date 2026-10-06 --no-send --output-dir ./reports/preview
```

This command requires separately authorized, newly rotated provider keys. The reporter rejects `--no-send` when that date already has a delivery marker in the chosen output directory, including with `--force-resend`, so an inspection cannot overwrite the immutable delivery snapshot.


Python tests mock network calls and cover successful upload, body-signing vectors, invalid signature/expired timestamp rejection responses, duplicate retry, malformed success responses, delivery failures, redirect safety, bounded files and durable snapshot/marker behavior. The Node harness executes the actual `Code.gs` with local doubles for Google services, including Drive, Properties, Utilities and LockService. It exercises authentication, replay expiration, payload tampering, duplicate/conflict handling, partial creation/replacement failures and read-free GET behavior. No test uses a deployment credential or makes production requests.

Historical local verification on 2026-10-09 passed all 61 Python tests and 28 Node receiver scenarios. The isolated Docker image ran 61 Python tests: 58 passed and three Node-dependent tests were skipped, with those tests passing on the host. A network-disabled container running its default `sleep infinity` command accepted the demo through `docker exec` as the unprivileged reporter user; PDF/JSON files in a named report volume survived container recreation. Temporary test containers and volumes were then removed. The helper passed offline, and `--live` without a process secret failed before network access; no Google POST was performed during those offline checks. Independent GetNewsAPI validation passed 505 tests with 62 infrastructure-dependent skips against a snapshot of its 148 unchanged public source-file hashes. The later user-reported live result is recorded above. Publication is now authorized; Dokploy deployment and billing smoke tests remain pending separate authorization.

The Node harness cannot prove Google's actual runtime, account consent, Drive permissions, redirect behavior, service availability, concurrency or quotas. Use the guarded TEST helper and dedicated private test parent described above to verify actual Google delivery without billing calls. Ordinary demo outputs remain rejected. Offline regression tests cover partial failures, expired/replayed requests and production force replacement; the live TEST helper intentionally forbids replacement. Never log the secret, signature, body or report contents. Provider billing smoke tests require newly rotated billing keys and separate authorization.

Build and test the Docker image and its exact scheduled command before production. An offline container command such as `python /app/app.py --demo --date 2026-10-06 --output-dir /tmp/reports` tests dependencies and PDF generation; it does not prove live provider access or scheduled execution. Do not provision, deploy or push merely to run these checks.

For an isolated reporter image, run this build command from the **CoinCourier API repository root**:

```bash
docker build -f services/ai-cost-reporter/Dockerfile -t ai-cost-reporter:local services/ai-cost-reporter
```

Then run an offline demo inside that image:

```bash
docker run --rm --network none ai-cost-reporter:local python /app/app.py --demo --date 2026-10-06 --output-dir /tmp/reports
```

The final build argument selects the [Docker build context](https://docs.docker.com/build/concepts/context/); using the repository root as the reporter context would mix the two applications. The existing GetNewsAPI root build remains independent and copies only its explicit application/maintenance paths. The reporter build contains only this service's runtime files.

## Billing compatibility and legacy OAuth backend

The existing billing implementation is retained. [OpenAI's Costs API](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs) documents inclusive starts, exclusive ends, daily USD buckets, project filtering and cursor pagination. [xAI Management Billing](https://docs.x.ai/developers/rest-api-reference/management/billing) documents the team USD analytics endpoint and `limitReached`; truncated responses are rejected. Billing finalization and scope must still be checked with an authorized live request before claiming operational accuracy.

**Numeric precision remains an accuracy limitation:** billing responses currently use default `response.json()` floating-point decoding before amounts are converted with `Decimal(str(value))`. High-precision JSON numeric values can lose source digits during decoding; Decimal aggregation cannot restore those digits. This delivery work preserves the existing billing code.

**OpenAI project-filter encoding remains a known compatibility check:** offline inspection of installed official Python SDK 1.70.0 showed bracketed arrays (`project_ids[]=...`, `group_by[]=line_item`); the existing `requests` implementation sends repeated unbracketed parameter names. The [official Python API reference](https://developers.openai.com/api/reference/python/resources/admin/subresources/organization/subresources/usage/methods/costs) confirms an array but does not establish server acceptance of the current encoding. This delivery change does not claim live project-scoped totals are verified.

For existing installations, an unset `GOOGLE_DRIVE_BACKEND` defaults to **`oauth`** for compatibility. The updated public `.env.example` explicitly demonstrates `apps_script` and its two required protected variables, while retaining optional legacy settings. The retained OAuth backend uses `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` and `GOOGLE_DRIVE_FOLDER_NAME`, with its original `YYYY / YYYY-MM` folder convention. It is configured separately and is unnecessary for Apps Script.

The legacy [scripts/authorize_google_drive.py](scripts/authorize_google_drive.py) and `requirements-oauth.txt` remain available only for separately authorized OAuth setup. Keep the Desktop client JSON outside this repository in Downloads and the helper output in ignored `.secrets/google-drive.env`; never mount either into the remote image. Only protected environment values reach Dokploy. The old `drive.file` authorization uses [Google's installed-app OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app) and [Drive file uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads). An External Gmail OAuth app in Testing has refresh-token lifetime limitations; review [Google's refresh-token expiration guidance](https://developers.google.com/identity/protocols/oauth2#expiration) before relying on that legacy backend.
