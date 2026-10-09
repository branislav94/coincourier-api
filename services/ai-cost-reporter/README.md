# CoinCourier — Daily AI Cost Reporter

A small Python 3.12 Docker/Dokploy daily job retrieves **actual provider-recorded OpenAI and xAI API costs**, generates a PDF and exact-precision JSON, and delivers both to CoinCourier Google Drive through an authenticated Google Apps Script web app. Email and WhatsApp remain optional.

## Location and service isolation

This service lives at **`coincourier-api/services/ai-cost-reporter/`** in the [CoinCourier API repository](https://github.com/branislav94/coincourier-api), independently of the existing GetNewsAPI application. It has its own Dockerfile, requirements, entry point, offline tests, environment settings and persistent report volume. The original standalone `ai-cost-reporter` project is retained intact. No credentials, `.env`, virtual environments, generated reports, delivery markers or Git metadata were copied; the operator-maintained `.env.example` template was copied unchanged.

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

The old `scripts/publish_to_github.ps1` helper is intentionally omitted. It initializes and publishes a standalone repository, which could create nested Git metadata inside the monorepo. The separately authorized legacy OAuth helper remains available. Publication is authorized to the CoinCourier repository's `dev` branch; deployment, account consent and live API tests require separate authorization. The original `api-test` checkout and its pending files remain untouched.

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
- Apps Script delivery requires a new random shared secret in **two permanent stores only**: Apps Script Script Properties and protected Dokploy environment variables. It requires no Desktop OAuth client JSON or Python OAuth tokens. Leave `GOOGLE_OAUTH_*` empty for this backend; earlier OAuth secrets are not used.

## One-time Google Apps Script setup

These are manual setup instructions for the owner. No deployment, cloud authorization or production upload is performed by the offline tests.

1. Sign in to [Google Apps Script](https://script.google.com/) using the **CoinCourier Google account that owns the target My Drive**. Create a new standalone project, for example `CoinCourier AI Cost Receiver`. Keep editing access limited to trusted administrators; editors can read Script Properties.
2. Replace the editor's `Code.gs` contents with [google_apps_script/Code.gs](google_apps_script/Code.gs). Open **Project Settings**, enable **Show "appsscript.json" manifest file in editor**, return to the editor, and replace its manifest with [google_apps_script/appsscript.json](google_apps_script/appsscript.json). Save both files.
3. Confirm **Services → Drive API**, version **v3**, identifier **Drive**, is enabled. The supplied manifest enables this Advanced Drive service. It is required by `authorizeSetup` and by `Drive.Files.update` when replacing existing PDF/JSON files without changing their IDs. With the automatically created default Cloud project, Google enables the API automatically. If you attach a standard Google Cloud project, manually enable **Google Drive API** in that project's Cloud Console. See [Google's advanced-service instructions](https://developers.google.com/apps-script/guides/services/advanced).
4. Generate **32 random bytes encoded as exactly 64 lowercase hexadecimal characters** privately. Do not save the generated value in a password manager, file or terminal transcript. On Windows, first disable **Clipboard history** and **Sync across devices** in **Settings → System → Clipboard**. The following PowerShell code uses the system cryptographic generator and copies the result directly to the clipboard; it does not print it or write a file. Run it manually only when ready to populate both private stores:

   ```powershell
   $aiCostRandom = [System.Security.Cryptography.RandomNumberGenerator]::Create()
   $aiCostBytes = New-Object byte[] 32
   try {
       $aiCostRandom.GetBytes($aiCostBytes)
       $aiCostSecret = [BitConverter]::ToString($aiCostBytes).Replace('-', '').ToLowerInvariant()
       Set-Clipboard -Value $aiCostSecret
   } finally {
       $aiCostRandom.Dispose()
       [Array]::Clear($aiCostBytes, 0, $aiCostBytes.Length)
       Remove-Variable aiCostSecret -ErrorAction SilentlyContinue
       Remove-Variable aiCostBytes, aiCostRandom
   }
   ```

   Paste into Script Properties and protected Dokploy configuration as described below, then clear the clipboard with `Set-Clipboard -Value ''`. Do not use a password, example test values, or an earlier OAuth secret. The code uses the literal 64-character text as the HMAC key, without hex-decoding it.
5. Open **Project Settings → Script Properties → Add script property**, then save the following properties. Google documents this interface in [Properties Service](https://developers.google.com/apps-script/guides/properties).

   | Property | Value |
   | --- | --- |
   | `AI_COST_SHARED_SECRET` | The new private 64-character lowercase hex secret. |
   | `AI_COST_PARENT_FOLDER_ID` | Optional: ID of a private parent folder owned by CoinCourier in My Drive. Leave absent/empty to use My Drive root. |

   The receiver creates `AI Infrastructure Costs / YYYY / MM` underneath that parent automatically. Supply the parent folder ID alone, without its URL. Keep the parent and existing child hierarchy private: Drive files inherit parent permissions. Do not point this receiver at a Shared Drive or a publicly shared folder. If duplicate same-name folders already exist, the receiver fails so an administrator can resolve the ambiguity.
6. In the editor's function selector choose **authorizeSetup**, then **Run**. Review and grant the requested Drive permission as CoinCourier. This function checks configuration and access without creating reports. Apps Script manages its own owner authorization; its manifest requests `https://www.googleapis.com/auth/drive`, which is broader than the legacy backend's `drive.file`. Review that permission accordingly. [DriveApp authorization documentation](https://developers.google.com/apps-script/reference/drive/drive-app) specifies the scope needed for folder/file creation.
7. When ready to deploy, select **Deploy → New deployment → Web app**. Choose **Execute as: Me (CoinCourier)** and **Who has access: Anyone**, including callers who are not signed in. Confirm the deployment. The public endpoint authenticates uploads with the body HMAC; it does not make Drive files public. If organizational policy does not permit anonymous web-app access, this backend cannot be used with these settings. See [Google's web-app deployment and execution-identity guide](https://developers.google.com/apps-script/guides/web).
8. Copy the deployed HTTPS URL ending in **`/exec`** into Dokploy's `GOOGLE_APPS_SCRIPT_WEB_APP_URL`. Use the original `https://script.google.com/macros/s/.../exec` URL without query parameters. `/dev` is restricted to script editors and is unsuitable for the daily job.
9. Set Dokploy's protected `GOOGLE_APPS_SCRIPT_SHARED_SECRET` to the **same literal secret** as `AI_COST_SHARED_SECRET`. Do not put the secret in the URL, request headers, source code or a checked-in `.env` file. Clear the clipboard after pasting into both stores.

After changing receiver code, save it and use **Deploy → Manage deployments → Edit → New version → Deploy** for the existing deployment. Its URL remains stable. A saved editor change alone does not update a versioned web app; see [Google's deployment guide](https://developers.google.com/apps-script/concepts/deployments). Rotate the shared secret by updating both private stores together. Keep the Dokploy host clock synchronized.

`doGet` returns only `{"ok":false,"error":"method_not_allowed"}`. It never accesses Drive or returns reports, names, folder IDs, configuration or credentials.

## Dokploy configuration

After deployment is separately authorized, configure a **separate Dokploy Application for this reporter** using the CoinCourier API repository. Retain existing GetNewsAPI deployment settings and services. Configure the reporter build fields as follows:

| Reporter build setting | Value |
| --- | --- |
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

Enter `GOOGLE_APPS_SCRIPT_WEB_APP_URL`, `GOOGLE_APPS_SCRIPT_SHARED_SECRET`, newly rotated `OPENAI_ADMIN_KEY`, `XAI_MANAGEMENT_KEY`, `XAI_TEAM_ID` and optional `OPENAI_PROJECT_IDS` through this reporter application's protected Dokploy environment configuration. **Set `GOOGLE_DRIVE_BACKEND=apps_script` explicitly.** The retained `.env.example` is the operator's OAuth-oriented template and was copied byte-for-byte; it does not list all new Apps Script backend values or automatically select that backend. Use the explicit settings above and add the two `GOOGLE_APPS_SCRIPT_*` values privately. Copying the template does not load an environment file into Python automatically. `GOOGLE_DRIVE_FOLDER_NAME` configures the legacy OAuth backend only; the Apps Script folder name is fixed.

Mount persistent storage at **`/data/reports`** and run **one replica**. Retain both reports and `.delivery.json` markers across restarts. If this later replaces an existing reporter deployment, retain its report volume and keep only one reporter schedule active during the cutover; do not copy reports or markers into Git or the Docker build context. The reporter volume stays separate from GetNewsAPI storage. The container's `sleep infinity` process keeps it available for scheduled commands; Python needs no incoming server, public domain or open port.

Configure one Application Schedule Job:

| Setting | Value |
| --- | --- |
| Cron expression | `0 9 * * *` |
| Timezone | `Europe/Belgrade` |
| Command | `python /app/app.py` |

The job runs at Serbian local 09:00, while each report covers a completed **UTC billing day**. Check the installed Dokploy scheduler's timezone support and the next displayed execution time before enabling the job. [Dokploy's schedule-job guide](https://docs.dokploy.com/docs/core/schedule-jobs) describes running commands inside application containers.

## Authentication protocol and bounded reports

Python sends an `application/json` envelope with exactly these fields:

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

The Node harness cannot prove Google's actual runtime, account consent, Drive permissions, redirect behavior, service availability, concurrency or quotas. After explicit authorization, use a separate private test destination to check the deployed runtime: authorize the owner, POST a completed provider-report pair from a separately authorized billing preview through the Python publisher, resend with fresh authentication and check unchanged file IDs, retry a partially created pair, and verify invalid signature/expired/replayed requests create nothing. Demo outputs are deliberately rejected by both publisher and receiver. Check force replacement preserves IDs. Never log the secret, signature, body or report contents. Provider/Drive smoke tests require newly rotated billing keys and separate authorization.

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

**OpenAI project-filter encoding remains a known compatibility check:** offline inspection of installed official Python SDK 1.70.0 showed bracketed arrays (`project_ids[]=...`, `group_by[]=line_item`); the existing `requests` implementation sends repeated unbracketed parameter names. The [official Python API reference](https://developers.openai.com/api/reference/python/resources/admin/subresources/organization/subresources/usage/methods/costs) confirms an array but does not establish server acceptance of the current encoding. This delivery change does not claim live project-scoped totals are verified.

For existing installations, an unset `GOOGLE_DRIVE_BACKEND` defaults to **`oauth`** for compatibility. The unchanged operator-maintained `.env.example` describes that legacy setup; Apps Script requires explicitly setting `GOOGLE_DRIVE_BACKEND=apps_script` and its two protected variables as described above. The retained OAuth backend uses `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` and `GOOGLE_DRIVE_FOLDER_NAME`, with its original `YYYY / YYYY-MM` folder convention. It is configured separately and is unnecessary for Apps Script.

The legacy [scripts/authorize_google_drive.py](scripts/authorize_google_drive.py) and `requirements-oauth.txt` remain available only for separately authorized OAuth setup. Keep the Desktop client JSON outside this repository in Downloads and the helper output in ignored `.secrets/google-drive.env`; never mount either into the remote image. Only protected environment values reach Dokploy. The old `drive.file` authorization uses [Google's installed-app OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app) and [Drive file uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads). An External Gmail OAuth app in Testing has refresh-token lifetime limitations; review [Google's refresh-token expiration guidance](https://developers.google.com/identity/protocols/oauth2#expiration) before relying on that legacy backend.
