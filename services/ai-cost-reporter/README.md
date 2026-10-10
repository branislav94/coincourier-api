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

The old `scripts/publish_to_github.ps1` helper is intentionally omitted. It initializes and publishes a standalone repository, which could create nested Git metadata inside the monorepo. The separately authorized legacy OAuth helper remains available. Google delivery was published at `7786e1d`, and billing-accounting corrections at `94507895cf460f3ec215a2f011a61ed5b0ff24e4`. The user now authorizes final reporter publication to CoinCourier `dev` after verification. This scope excludes Dokploy deployment/scheduling, Google deployment/property changes, new agent live calls and report-generation jobs. Preserve the existing Apps Script deployment and configured secret; do not generate or rotate credentials. The original `api-test` checkout and its pending files remain untouched.

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

- Use an existing, newly rotated OpenAI **organization Admin API key** with access to organization costs and an xAI **Management key** with billing-read access for the intended team. Previously disclosed keys must be revoked and replaced before any separately authorized live check or deployment. No credential generation or retrieval is part of the current work.
- `OPENAI_PROJECT_IDS` blank means the OpenAI organization; a comma-separated list requests only those projects. Scoped responses must identify a requested project on every result. xAI always covers the configured team, without an assumed project breakdown. The user reports successful xAI no-recorded-usage classification; OpenAI live/project-scope verification and final reconciliation remain pending.
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

Publication does not deploy this service. After deployment is separately authorized, create a **separate Dokploy Application for this reporter** using the CoinCourier API repository. Retain existing GetNewsAPI deployment settings/services and the existing Apps Script `/exec` deployment and secret. Keep reporter AutoDeploy and its schedule disabled during setup. Configure the reporter build fields as follows:

| Reporter build setting | Value |
| --- | --- |
| Repository | `branislav94/coincourier-api` |
| Branch | `dev` |
| Build type | `Dockerfile` |
| Repository build path | `/` |
| Dockerfile Path | `services/ai-cost-reporter/Dockerfile` |
| Docker Context Path | `services/ai-cost-reporter` |
| Docker Build Stage | Empty |
| Startup command override | Unset; retain the image's `sleep infinity` |
| Replicas | `1` |

Check the installed Dokploy version's resulting build command uses these paths; [Dokploy documents separate Dockerfile and context fields](https://docs.dokploy.com/docs/core/applications/build-type). The context must be the service directory so its own `.dockerignore` and requirements apply. Use an independent reporter image and volume, with no news database connection, incoming server, public domain or exposed port. `python /app/app.py` belongs in the scheduled command, while `sleep infinity` keeps the target container running. Do not turn the billing job into the container startup command.

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

In the reporter application's mounts, attach a dedicated persistent named volume at **`/data/reports`** and run **one replica**. Retain both reports and `.delivery.json` markers across restarts. If this later replaces an existing reporter deployment, retain its report volume and keep only one reporter schedule active during the cutover; do not copy reports or markers into Git or the Docker build context. The reporter volume stays separate from GetNewsAPI storage. The container's `sleep infinity` process keeps it available for scheduled commands; Python needs no incoming server, public domain or open port.

The volume must be writable by the container's unprivileged reporter user. The validated image uses UID 999; inspect the actual deployed UID/GID before assigning ownership to a new volume or bind mount. Correct permissions only on that dedicated reporter storage, retaining existing snapshots; do not make it world-writable or change GetNewsAPI storage. After an approved deployment, the following container-terminal checks inspect identity/access and print an offline plan without reading credential values, creating reports or making network requests:

```bash
id
python -c "import os; print('reports_directory_writable=' + str(os.path.isdir('/data/reports') and os.access('/data/reports', os.W_OK | os.X_OK)))"
python /app/app.py --help
python /app/scripts/billing_smoke_test.py --date 2026-10-06 --diagnostic --provider both
```

Deploy the reporter only after separate approval, then keep the schedule disabled while checking the running `sleep infinity` container, its protected environment configuration and persistent writable mount. The date above is an example completed UTC day, not a claim about the user's final live verification period. Before approving a production run, complete the still-pending OpenAI read-only cost/project-scope check and Google owner inspection of the existing TEST files; confirm manual TEST-gate disabling without replacing the Google deployment or secret. Any live check or first real PDF/JSON upload needs its own authorization. Verify volume persistence across later container recreation and preserve marked snapshots.

Configure one Application Schedule Job:

| Setting | Value |
| --- | --- |
| Cron expression | `0 9 * * *` |
| Timezone | `Europe/Belgrade` |
| Command | `python /app/app.py` |
| Enabled | `false` until scheduling is separately approved |

The job runs at Serbian local 09:00, while each report covers a completed **UTC billing day**. [Dokploy's schedule-job guide](https://docs.dokploy.com/docs/core/schedule-jobs) describes execution inside a running application container, and [its schedule API](https://docs.dokploy.com/docs/api/schedule) exposes per-job `timezone` and `enabled` fields. Set the per-job timezone, check the installed UI/version's next execution time including daylight-saving changes, and keep Enabled off until scheduling approval. Setting `TZ` inside the reporter container does not configure the scheduler; do not change the shared Dokploy/server timezone or existing news schedules to compensate for an unsupported per-job setting. If that setting is unavailable, leave this schedule disabled until supported configuration is established. Once separately approved, enable exactly one reporter schedule and monitor its exit status plus validated two-file delivery; HTTP 200 alone is insufficient.

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

Final offline publication verification on 2026-10-10 passed **196/196 Python tests and 28/28 Node scenarios**. Image `ai-cost-reporter:publication-validation-20261010` built from the exact service context and discovered 196 tests: 193 passed and three Node-dependent tests were skipped, with those three passing on the host. With networking disabled, default, xAI-only and both-provider offline plans, imports and the scheduled CLI's `--help` passed inside a running UID 999 `sleep infinity` container. Runtime `app.py` and helper hashes matched the worktree; `/data/reports` remained empty, no secrets were bundled, and owned test containers were cleaned up. All 148 public news source hashes were rechecked unchanged; the prior 505-pass/62-skip GetNewsAPI suite remains a historical result. Publication receipts and any remote CI result are reported separately after actual publication. No new agent live call, report/scheduled job, Google change or deployment was performed.

Historical local accounting-state verification on 2026-10-10 passed **196/196 Python tests and 28/28 Node scenarios**. Docker image `ai-cost-reporter:inactive-validation-20261010` discovered 196 tests: 193 passed and three Node-dependent tests were skipped, with those three passing on the host. With networking disabled, default, xAI-only and both-provider offline plans passed. A container ran `sleep infinity` as UID 999; `python /app/app.py --help` checked the scheduled entry-point parser without executing the job, and `/data/reports` stayed empty. Runtime `app.py` and billing-helper hashes matched the worktree. Independent GetNewsAPI validation passed 505 tests with 62 infrastructure-dependent skips; all 148 public source hashes matched. No agent live provider calls, credential inspection, report-generation/scheduled job, Google operation, commit, push or deployment was performed during that phase. The later user-reported corrected live result is recorded below.

Historical xAI zero-confirmation verification on 2026-10-10, before the distinct no-recorded-usage classification, passed **176/176 Python tests and 28/28 Node scenarios**. Docker image `ai-cost-reporter:xai-zero-validation-20261010` discovered 176 tests: 173 passed and three Node-dependent tests were skipped, with those three passing on the host. With networking disabled, the bundled default and xAI-only diagnostic offline plans passed; the image ran as UID 999, retained `sleep infinity`, and `/data/reports` remained empty. Its four executable source hashes matched the worktree. Independent GetNewsAPI validation discovered 567 tests: 505 passed and 62 database/tool-dependent tests were skipped; all 148 public source hashes matched. No reporter/demo job, report generation, live provider call, credential inspection, Google operation, commit, push or deployment was performed; tests used standard synthetic fixtures only.

Historical diagnostic verification on 2026-10-10, before the empty-series confirmation change, passed **152/152 Python tests and 28/28 Node scenarios**. Docker image `ai-cost-reporter:billing-diagnostics-20261010` discovered 152 tests: 149 passed and three Node-dependent tests were skipped, with all three passing on the host. With networking disabled, the bundled `--diagnostic --provider both` offline plan passed. A separate container kept its default `sleep infinity` command running while the unprivileged reporter user ran the demo through `docker exec`; the valid PDF and `SYNTHETIC_DEMO` JSON had no delivery marker. That temporary container was removed. The four executable source-file hashes matched the worktree. No live provider calls or credential inspection, new Google changes, commit, push or deployment occurred. GetNewsAPI was not rerun or modified in that diagnostic phase.

Historical billing-accounting verification for published commit `94507895cf460f3ec215a2f011a61ed5b0ff24e4` on 2026-10-10 passed **106 Python tests and 28 Node scenarios**. The isolated Docker image `ai-cost-reporter:billing-validation-20261010` discovered 106 tests: 103 passed and three Node-dependent tests were skipped, with those three passing on the host. With networking disabled, its bundled smoke helper's default plan and non-root demo passed while the unchanged `sleep infinity` command kept the container running. The production command rejected absent billing keys before provider access. Independent GetNewsAPI validation passed 505 tests with 62 infrastructure-dependent skips, and all 148 public source-file hashes matched. These checks precede the current diagnostic changes; no live provider requests, new Google changes or deployment were performed.

Historical local verification on 2026-10-09 passed all 61 Python tests and 28 Node receiver scenarios. The isolated Docker image ran 61 Python tests: 58 passed and three Node-dependent tests were skipped, with those tests passing on the host. A network-disabled container running its default `sleep infinity` command accepted the demo through `docker exec` as the unprivileged reporter user; PDF/JSON files in a named report volume survived container recreation. Temporary test containers and volumes were then removed. The helper passed offline, and `--live` without a process secret failed before network access; no Google POST was performed during those offline checks. Independent GetNewsAPI validation passed 505 tests with 62 infrastructure-dependent skips against a snapshot of its 148 unchanged public source-file hashes. The later user-reported live results are recorded separately. This historical integration phase is complete; deployment and scheduling still require separate authorization.

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

## Billing accounting and completeness

Billing uses actual provider-recorded USD consumption, with daily and month-to-date totals for the same scope. OpenAI uses `GET https://api.openai.com/v1/organization/costs` with an organization Admin key; [OpenAI's Costs API](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs) defines inclusive Unix-second starts, exclusive ends, daily buckets, project filtering and cursor pagination. [Administration guidance](https://developers.openai.com/api/reference/administration/overview) confirms the Admin-key requirement.

xAI uses the read-only analytics `POST https://management-api.x.ai/v1/billing/teams/{team_id}/usage` with a separate Management key. The body requests `usd` with `AGGREGATION_SUM`, `TIME_UNIT_DAY`, `groupBy=["description"]`, no additional filters and `timezone="Etc/GMT"`. This POST reads historical usage; it does not update billing settings or make an inference request. The [expanded official billing schema](https://docs.x.ai/developers/rest-api-reference/management/billing.md) defines an exclusive `endTime` and dense daily points, so the request ends at the **next UTC midnight**, including the complete final day. The example ending at `23:59:59` is not used. `limitReached=true` means a truncated result.

Provider JSON numeric values now decode directly to `Decimal` from the raw JSON, before any floating-point conversion. Validated exact aggregation preserves source decimal digits and does not depend on the ambient Decimal context. JSON and smoke-summary amounts retain full fixed-point decimal text; PDF monetary display rounds to cents only at presentation. Non-finite or malformed monetary values fail. This corrects the earlier float-decoding precision limitation.

OpenAI arrays use the official SDK's bracketed wire convention: repeated `project_ids[]` and `group_by[]`. The unfiltered request groups by line item; a scoped request also groups by project ID and rejects missing/null IDs or IDs outside the requested list. [The official API reference](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs) documents arrays, while offline SDK/request tests establish the chosen serialization. Actual server acceptance and project-scoped totals still need an authorized live check.

Completeness checks are deliberately strict. OpenAI must return unique, correctly bounded daily UTC buckets for every requested month-to-date day and complete pagination with explicit `has_more=false`; a terminal cursor may be null or omitted. An explicit covered bucket with empty results may be zero; missing buckets are unknown. A nonempty xAI grouped response must explicitly return `limitReached=false` and exactly one UTC-midnight point per requested day in **each** unique description series. Missing/duplicate dates, invalid values, incomplete or truncated data fail; they are not converted to $0 or retried as ungrouped data.

An explicit, otherwise valid `timeSeries=[]` with `limitReached=false` triggers **one additional read-only USD confirmation query** to the same xAI endpoint. It retains the same team, UTC period, daily granularity, USD SUM and empty filters, changing only `groupBy` from `["description"]` to `[]`. An explicit zero-spending confirmation must return exactly one ungrouped aggregate series, explicit `limitReached=false`, every requested UTC-midnight date exactly once and an explicit finite numeric USD **zero at every point**. Present group/group-label fields must be empty arrays; omitted group labels are allowed for this ungrouped query. Nonzero values, including positive/refund values that sum to zero, fail with `aggregation_mismatch` at coverage.

If both queries instead return valid explicit empty datasets, with explicit `limitReached=false` and the identical validated request scope, the application classifies **`NO_RECORDED_USAGE`**. Each empty envelope must contain exactly the expected `timeSeries` and `limitReached` fields; unknown scope/error/cursor fields are rejected. Its amounts mean **0 USD recorded in these returned datasets as of this check**, not explicit daily zero records or fully reconciled zero consumption. No daily points or model breakdown are invented. Missing fields, malformed/truncated/partial data, conflicting metadata, authentication/permission errors and failed reads remain `UNKNOWN` and block the report.

| Accounting state | Evidence | Amount meaning |
| --- | --- | --- |
| `RECORDED_SPENDING` | Complete validated provider records/coverage, including explicit zero or nonzero spending. | The provider-recorded USD total. |
| `NO_RECORDED_USAGE` | Both valid nontruncated xAI queries returned explicit empty datasets for the same requested team and UTC period. | 0 USD recorded as of the observation; consumption and final reconciliation remain unresolved. |
| `UNKNOWN` | A failed, missing, malformed, partial, truncated or conflicting response. | No trustworthy amount; fail rather than report $0. |

The [xAI schema](https://docs.x.ai/developers/rest-api-reference/management/billing.md) documents dense points within returned series, but does not promise that an empty grouped list means zero or that an inactive team produces an ungrouped aggregate series. `NO_RECORDED_USAGE` follows the user's explicitly authorized application semantics for record absence. The documented response does not echo team, period, metric or timezone; provenance identifies the validated request scope and does not claim independent response-echo verification. It uses billing datasets rather than token/request counts, invoice previews or credit balances. The user reports a successful corrected live classification for their checked period; this does not establish final billing reconciliation or every future provider response shape.

An explicit dense-zero confirmation retains `accounting_evidence="ungrouped_dense_zero_confirmation"`. An inactive xAI JSON snapshot adds `accounting_state="NO_RECORDED_USAGE"`, `accounting_evidence="grouped_and_ungrouped_empty"`, `reconciliation_status="not_reconciled"` and `allow_recheck=true`, with recorded daily/month-to-date USD `"0"` and an empty breakdown. These fields are validated when snapshots are read again. The PDF visibly labels xAI **no recorded usage** and its combined amount **RECORDED TOTAL**, with no-records, late-billing and recheck caveats; optional email includes the same qualified notes. Normal recorded responses and demo JSON stay compatible. Up to two xAI billing reads occur, or up to three total requests in the xAI diagnostic including Management-key validation. These are bounded read-only requests, not an automatic fallback for other errors.

[xAI's Management guide](https://docs.x.ai/developers/management-api-guide) distinguishes the Management key from an inference key. Select the minimum team-scoped billing-read access available in the current console and verify its actual permission names before a live check. [Usage Explorer](https://docs.x.ai/console/usage) defaults to USD consumption. Compare usage consumption rather than credit purchases, prepaid balances or invoice amounts including tax. Billing finalization and scope must still be reconciled with an authorized live request before claiming production accuracy.

## Read-only billing smoke check and console reconciliation

[scripts/billing_smoke_test.py](scripts/billing_smoke_test.py) defaults offline. It prints a `prepared_offline` plan for the selected date without reading provider credential, team or project environment values, loading `.env` or making requests. The default date is the previous complete UTC day; an explicit date must also be a completed day. From the service directory:

```powershell
python .\scripts\billing_smoke_test.py --date 2026-10-06
python .\scripts\billing_smoke_test.py --date 2026-10-06 --diagnostic --provider xai
```

The xAI-only offline plan lists only its Management-key/team environment names and describes the conditional ungrouped confirmation; it reads none of those values and dispatches no request.

**User-reported final xAI live verification:** Management-key validation accepted the key and matched the configured team; grouped and ungrouped billing queries returned HTTP 200. The corrected billing check returned `recorded_usage_absent`, `accounting_state=NO_RECORDED_USAGE`, recorded daily USD 0, recorded month-to-date USD 0 and `reconciliation_status=not_reconciled`. Overall status was `diagnostics_recorded_usage_absent`, with exit code 0. The user reports dashboard zeros for the corresponding period. This validates the user's checked no-recorded-usage path; it does not claim fully reconciled zero consumption, successful OpenAI billing or agent inspection of the live responses/account. Execution date, exact report date and raw schema details were not supplied and are not inferred here.

The user reports a corresponding dashboard period; the agent did not independently inspect console timezone/filter settings. The example below uses completed day October 6 for any later separately authorized recheck and does not state that this was the final live verification's report date.

`--diagnostic` adds independent `openai_costs`, `xai_management_key_validation` and `xai_billing` checks. `--provider both|openai|xai` selects diagnostic checks only; legacy `--live` retains its requirement for validated results from both providers. Diagnostic mode also defaults offline and reads no environment values without `--live`. Live diagnostic output allows only structured request counts/statuses, failure stage/category/code, schema/completeness flags, missing UTC days, pagination state and whitelisted schema types/counts. Unknown amounts remain unknown; explicit covered zero and no-recorded-usage amounts carry their distinct states. Diagnostics never calculate a combined total or print raw provider field names/labels, keys, headers or private IDs.

`confirmed_zero` means the completely covered period's explicit USD cost total is zero. It does not establish that no requests or tokens were used. `NO_RECORDED_USAGE` describes successful record absence instead; its recorded amount must not be presented as a fully reconciled zero.

For the inactive state, `xai_billing.status` is `recorded_usage_absent`, `accounting_state` is `NO_RECORDED_USAGE`, both usage states are `no_recorded_usage`, and `coverage` is `valid_empty_datasets`. It records `zero_confirmation_attempted=true` and `zero_confirmation_confirmed=false`, together with `reconciliation_status="not_reconciled"` and `allow_recheck=true`. If all selected checks are accepted, the overall diagnostic status is `diagnostics_recorded_usage_absent` with exit code 0. Explicit covered spending retains `diagnostics_confirmed`; errors remain failures.

The xAI billing checks' `response_schemas` contain only whitelisted field types/counts, JSON status, query mode and completeness booleans. They inspect billing POST responses, not Management-key metadata. For valid absence they show grouped/ungrouped query modes, `json_status="valid_object"`, `limit_reached=false`, zero series/point/value counts and no unexpected top-level fields. `metadata_complete` means bounded schema inspection completed; it does not replace accounting validation or prove daily zero coverage. Unknown field names, raw monetary/body values, labels, HTTP headers, private identifiers and credentials are never printed by this response inspection.

`request_attempted` and `requests_attempted` record transport dispatch attempts, which do not prove the provider received a request. `http_statuses` records each attempt's HTTP status or `null` when none was received; `http_status` describes the final attempt. A timeout after a successful earlier page therefore reports a final `null`, without treating the earlier HTTP 200 as complete success.

Inspect each entry under `checks`. A failure at `preflight` with no request attempt identifies a local date/configuration check. HTTP rejection flags distinguish authentication and permission errors; response-schema, completeness and pagination flags identify responses that cannot establish a complete cost total. Such amounts remain unknown. A successful xAI key-validation check alongside failed billing is evidence about two separate endpoints; investigate the billing check's own stage, status and category separately.

The xAI key check uses `GET https://management-api.x.ai/auth/management-keys/validation`. [xAI's Management guide](https://docs.x.ai/developers/management-api-guide) states this validation requires no ACL permission; [its schema](https://docs.x.ai/developers/rest-api-reference/management/auth.md) returns key metadata. The helper validates a bounded JSON object with a nonempty `apiKeyId` and types of present scope/ACL fields, then reports only `key_scope` (`team`, `organization`, `unspecified` or `unknown`) and `team_linkage` (`match`, `mismatch`, `unknown` or `not_configured`). No key ID, key value, redacted key, owner, name, ACL list or response body is printed. Team scope must identify the configured team to establish a match; organization scope does not prove a particular team membership. An accepted key linked to the wrong team retains `key_accepted=true` while that validation check fails with `scope_mismatch`. Validation acceptance is separate from billing-read permission, so the billing check runs independently even when validation rejects.

Inference-key metadata lives at `https://api.x.ai/v1/api-key`, as described in [the inference account reference](https://docs.x.ai/developers/rest-api-reference/inference/other). Management credentials belong on the Management endpoint; a token's prefix does not prove its key type. Missing team configuration still permits Management-key validation while the billing check fails its own preflight.

The read-only allowlist contains OpenAI Costs GET, xAI Management-key validation GET and the documented xAI analytics POST, with TLS verification, redirects disabled and environment proxy/netrc settings disabled. The helper does not create reports, PDF/JSON files or markers, send notifications, upload to Drive, invoke models or alter billing configuration.

For a later separately authorized local xAI-only recheck, use a fresh PowerShell session in the reporter environment. This example prompts privately for the existing newly rotated xAI Management key and team ID, passes them only through named environment variables, and clears temporary values and native buffers even on failure. No OpenAI key is needed and its project filter is left untouched. The helper never overrides a configured filter. Do not paste credentials into chat or command arguments. No new live check is performed as part of publication.

```powershell
$aiCostBillingInputs = @{}
$aiCostBillingBuffers = @{}
$aiCostBillingExitCode = $null
try {
    foreach ($aiCostBillingName in @('XAI_MANAGEMENT_KEY', 'XAI_TEAM_ID')) {
        $aiCostBillingInputs[$aiCostBillingName] = Read-Host $aiCostBillingName -AsSecureString
        $aiCostBillingBuffers[$aiCostBillingName] = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($aiCostBillingInputs[$aiCostBillingName])
        [Environment]::SetEnvironmentVariable(
            $aiCostBillingName,
            [Runtime.InteropServices.Marshal]::PtrToStringBSTR($aiCostBillingBuffers[$aiCostBillingName]),
            'Process'
        )
    }
    python .\scripts\billing_smoke_test.py --date 2026-10-06 --diagnostic --live --provider xai
    $aiCostBillingExitCode = $LASTEXITCODE
} finally {
    foreach ($aiCostBillingName in @('XAI_MANAGEMENT_KEY', 'XAI_TEAM_ID')) {
        Remove-Item -LiteralPath ('Env:\' + $aiCostBillingName) -ErrorAction SilentlyContinue
    }
    foreach ($aiCostBillingBuffer in $aiCostBillingBuffers.Values) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($aiCostBillingBuffer)
    }
    foreach ($aiCostBillingInput in $aiCostBillingInputs.Values) {
        $aiCostBillingInput.Dispose()
    }
    Remove-Variable aiCostBillingInputs, aiCostBillingBuffers, aiCostBillingName, aiCostBillingBuffer, aiCostBillingInput -ErrorAction SilentlyContinue
}
Write-Output ('Diagnostic process exit code: ' + $aiCostBillingExitCode)
Remove-Variable aiCostBillingExitCode
```

The reporter image includes the helper at `/app/scripts/billing_smoke_test.py`. After both deployment and a live check are separately authorized, the corresponding running-container command inherits protected reporter environment settings:

```bash
docker exec REPORTER_CONTAINER python /app/scripts/billing_smoke_test.py --date 2026-10-06 --live
```

A successful legacy smoke result prints sanitized status, exact USD daily/month-to-date provider and combined totals, the UTC ranges, scope metadata and `generated_at_utc`. If xAI has no recorded usage, legacy status becomes `recorded_spending_snapshot` with the inactive provider metadata and top-level nonreconciliation/recheck caveats; it does not claim fully confirmed consumption. Diagnostic output separates each provider/check and emits no combined total; use its structured failure categories to identify the failed stage. Legacy errors retain the fixed `BILLING_SMOKE_FAILED` prefix. Neither mode prints raw provider bodies or traceback text. Observation timestamps describe query snapshots, not provider billing finality; record the console observation time as well. Match complete provider results against the consoles as follows:

1. Select the same OpenAI organization and exact project list, or all projects for a blank list. Select the configured xAI team with no extra API-key/model filters, using USD consumption rather than tokens or credit purchases.
2. Use UTC dates for both consoles. For report date `2026-10-06`, daily is `[2026-10-06T00:00:00Z, 2026-10-07T00:00:00Z)`; month-to-date is `[2026-10-01T00:00:00Z, 2026-10-07T00:00:00Z)`. The 09:00 Serbian job schedule does not change these boundaries.
3. Compare both providers' daily and month-to-date values individually before comparing their combined recorded total. Console display rounding can hide decimal digits; retain the helper's exact text privately and use an exact console export if needed. Explicit zero spending requires covered data. `NO_RECORDED_USAGE` means no returned records from two valid queries, with 0 USD recorded as of that check; keep the absence/finality caveat visible rather than treating console zeros as proof of final consumption. Failed/incomplete reads remain unknown.
4. Record the observation time and recheck the same old UTC day after late billing or corrections. The read-only helper always makes fresh API reads and may change `NO_RECORDED_USAGE` to `RECORDED_SPENDING` when records arrive. An ordinary reporter rerun preserves the delivered snapshot. An intentional reconciled replacement requires separately approved `--force-resend`; the read-only helper never modifies that snapshot or Drive.

## Legacy OAuth backend

For existing installations, an unset `GOOGLE_DRIVE_BACKEND` defaults to **`oauth`** for compatibility. The updated public `.env.example` explicitly demonstrates `apps_script` and its two required protected variables, while retaining optional legacy settings. The retained OAuth backend uses `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` and `GOOGLE_DRIVE_FOLDER_NAME`, with its original `YYYY / YYYY-MM` folder convention. It is configured separately and is unnecessary for Apps Script.

The legacy [scripts/authorize_google_drive.py](scripts/authorize_google_drive.py) and `requirements-oauth.txt` remain available only for separately authorized OAuth setup. Keep the Desktop client JSON outside this repository in Downloads and the helper output in ignored `.secrets/google-drive.env`; never mount either into the remote image. Only protected environment values reach Dokploy. The old `drive.file` authorization uses [Google's installed-app OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app) and [Drive file uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads). An External Gmail OAuth app in Testing has refresh-token lifetime limitations; review [Google's refresh-token expiration guidance](https://developers.google.com/identity/protocols/oauth2#expiration) before relying on that legacy backend.
