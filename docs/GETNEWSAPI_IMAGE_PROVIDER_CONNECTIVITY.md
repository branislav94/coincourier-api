# Image-provider live compatibility

## Openverse blocked-egress handling (2026-10-28)

The operator's DEV evidence at `77f4df90fd78ab1d4fd1371e94e3d234563e448f`
shows HTTP 403 HTML responses from both the official OAuth endpoint and an
anonymous image search, with Cloudflare server/ray headers. This is an edge
access denial before normal API handling, not proof of invalid credentials.
Six DEV publications succeeded with Pexels and `publish_failed=0`; that evidence
does not establish Openverse availability. Openverse documents both anonymous
access and OAuth client credentials in its [official API documentation](https://api.openverse.org/v1/).

There is no demonstrated code-only way to remove this egress block. The
infrastructure owner must arrange either:

- Openverse/WordPress approval or allowlisting for the fixed public DEV and
  production outbound IPs, including the OAuth and image-search endpoints; or
- a trusted outbound proxy, NAT or relay under CoinCourier's control, with
  reachable egress and permission to use the Openverse API from that egress.

Do not use challenge bypasses, browser cookies, public proxies, fake browser
identities, rate-limit circumvention or automatic proxy rotation. No approved
alternate route was provisioned or tested by this code change.

### Configuration

Defaults continue to use the official endpoints directly:

```dotenv
OPENVERSE_API_BASE_URL=https://api.openverse.org/v1/
OPENVERSE_TOKEN_URL=https://api.openverse.org/v1/auth_tokens/token/
OPENVERSE_HTTPS_PROXY=
OPENVERSE_ACCESS_TOKEN=
```

The base URL is the API prefix; the provider appends `images/`. Set the token
URL separately when an approved relay exposes a different endpoint. API/token
URLs require HTTPS. Endpoints and proxy URLs reject embedded credentials, query
strings and fragments. An approved proxy may use HTTP CONNECT or HTTPS, but must
not require credentials embedded in its URL; use an approved network-authenticated
route instead. For example, `OPENVERSE_HTTPS_PROXY=http://approved-egress.example:8080`
illustrates the format only; the operator must supply their approved host.

The explicit proxy applies only to Openverse OAuth/search calls, not other
providers, downloaded source images, WordPress or unrelated HTTP. Direct
Openverse calls explicitly ignore ambient `HTTP_PROXY`, `HTTPS_PROXY` and
`ALL_PROXY`; migrate an intentionally approved ambient route to this explicit
setting. Shared sessions and other providers' proxy/cache behavior are preserved.
Requests use ordinary verified HTTPS and do not follow API redirects.

Normal OAuth continues to use `OPENVERSE_CLIENT_ID` and `OPENVERSE_CLIENT_SECRET`.
With neither configured, anonymous search remains supported. Partial credentials,
OAuth rejection or malformed token responses remain provider failures; configured
OAuth no longer silently falls back to anonymous search. HTML 403 responses are
reported as `edge_access_denied`, with only status, allowlisted content type and
Cloudflare/ray-presence booleans. JSON OAuth 401 remains `authentication_failed`.
No response body, cookie, client identifier, secret, token or proxy credentials
are logged by these diagnostics.

`OPENVERSE_ACCESS_TOKEN` is an optional controlled diagnostic override. When
present, application searches use it as Bearer authentication and skip OAuth.
It is not written to the response cache or logs. Tokens expire; this is not a
permanent replacement for supported OAuth/approved network access. A token
obtained elsewhere may still be denied by the blocked DEV edge route. That
specific experiment must be run from DEV before claiming success; it was not
testable from this checkout.

Keep provider order, thresholds, reuse protection and generation policy unchanged.
An unavailable Openverse remains `provider_failed`, not confirmed exhaustion.
With `IMAGE_GENERATION_ON_PROVIDER_ERROR=false`, it cannot authorize generation
when no stock provider succeeds. Restart runtime processes after an approved
environment change; configuration is read at import time.

### Explicit safe DEV probes

Run these separately from offline unit tests, in the DEV container with its
runtime secrets supplied through the existing secret-management mechanism:

```sh
cd /app
python -B openverse_probe.py --path direct --mode oauth
python -B openverse_probe.py --path direct --mode anonymous
python -B openverse_probe.py --path direct --mode bearer
python -B openverse_probe.py --path configured --mode all
```

Do not pass credentials or tokens as command-line arguments. The bearer probe
is skipped unless `OPENVERSE_ACCESS_TOKEN` is supplied. The explicit OAuth probe
tests token acquisition even if that diagnostic override exists; the acquired
token is never printed, persisted or reused by other probe operations. The
anonymous probe deliberately tests reachability without authentication; it does
not change application fallback behavior.

`--path direct` ignores custom endpoints/proxy settings. `--path configured`
uses them. `--path both` compares both paths without automatic failover. Each
operation makes at most one uncached request with the common bounded timeout;
OAuth/bearer operations lacking credentials are skipped. The query defaults to
`bitcoin` and search requests only one result. No image generation, image download,
database or WordPress operation is performed.

Output is one sanitized JSON record per operation: path/action, outcome, HTTP
status, content-type category, Cloudflare/ray-presence flags, and result count or
token-received boolean. It never dumps API bodies, headers, endpoints or secrets.
Exit status is 0 for successful attempted operations, 1 if any operation fails,
and 2 if every operation is skipped. Return sanitized results plus the approved
route/IP information through the normal infrastructure review process.

No DEV environment or production configuration is changed by this document.
Stop after the requested local verification; deployment remains with DevOps.

## Wikimedia public metadata verification (2026-10-28)

Separate, public read-only Commons probes used the existing MediaWiki contract:
File-namespace search, ten results, imageinfo URL/size/MIME/canonicaltitle and the
current filtered extmetadata, with `maxlag=5`. TLS verification remained enabled.
No complete live payload, creator HTML or image bytes were saved in the repository.
These probes ran from the checkout environment, not from the DEV container.

The proven false-negative was the original-URL predicate
`not image_parts.query`. Real raster originals carry the three tracking keys
`utm_source`, `utm_campaign`, and `utm_content`. All other original URL predicates
passed on the sampled images. The fix permits only these observed keys on the
same validated `upload.wikimedia.org/wikipedia/commons/...` original path;
unknown query keys, transformations, malformed queries and fragments still fail.
The API-provided original URL is preserved.

| Exact query | Raw | Normalized before | Normalized after | Remaining rejections |
|---|---:|---:|---:|---|
| bitcoin cryptocurrency | 10 | 0 | 4 | 6 license rejections |
| bitcoin inflation | 7 | 0 | 1 | 6 unsupported formats |
| bitcoin dominance | 3 | 0 | 0 | 2 unsupported formats, 1 BY-SA license rejection |

Sanitized observed structures, with no attribution HTML:

| Field | Accepted JPEG | Rejected PNG |
|---|---|---|
| pageid / namespace | 92021811 / 6 | 103634264 / 6 |
| page title and canonicaltitle | `File:Bitcoin and cryptocurrency (38688311185).jpg` | `File:Bitcoin dominans grafigi.png` |
| imageinfo present | true | true |
| MIME | image/jpeg | image/png |
| width / height | 2000 / 1300 | 1400 / 825 |
| byte size | 3886027 | 61927 |
| original hostname | upload.wikimedia.org | upload.wikimedia.org |
| description hostname | commons.wikimedia.org | commons.wikimedia.org |
| numeric identity | wikimedia:92021811 | wikimedia:103634264 |
| file alias | `wikimedia:file:Bitcoin and cryptocurrency (38688311185).jpg` | `wikimedia:file:Bitcoin dominans grafigi.png` |
| identity conflict | false | false |
| before | required_fields: nonempty original query | required_fields: nonempty original query |
| after | normalized, CC BY 2.0 | license_rejected, CC BY-SA 4.0 |

Both samples contained Artist, AttributionRequired, Copyrighted, Credit,
ImageDescription, LicenseShortName, LicenseUrl, ObjectName, Restrictions and
UsageTerms in the filtered metadata. Their three URL query keys were the tracking
keys above. The JPEG's recognized license path was `/licenses/by/2.0/`; the PNG's
`/licenses/by-sa/4.0/` remains unsupported.

A WebP was also observed: page 156272632, `File:Bitcoin Price.webp`, 4827x1644,
105024 bytes, the same original/description hosts and tracking keys, with numeric
and File-title identities. Its URL now passes, but unchanged licensing checks
still reject it. Its complete metadata-key list and precise licensing predicate
were not retained, so no more specific licensing conclusion is asserted. The
inflation JPEG normalizes but remains 869x947 and fails unchanged downstream
dimensions/orientation safeguards.

No sampled result had conflicting numeric/File identities. The proposed broad
identity-conflict relaxation was therefore unnecessary and was not implemented.
The page's valid File title is retained, with a valid equivalent canonical title
added as an alias; the shared identity helper is unchanged. Contradictory numeric
IDs, unrelated hosts and genuinely conflicting titles still fail. Filenames are
not lowercased, and conservative percent/underscore handling is preserved.

Normalizer summaries now distinguish missing_page_id, invalid_namespace,
invalid_file_title, missing_imageinfo, invalid_canonical_title,
invalid_original_url, unrecognized_commons_identity and commons_identity_conflict,
alongside existing licensing/format/size/dimension rejections. No raw payloads
are logged. Strict licenses, formats, thresholds and attribution safeguards are
unchanged.

Sanitized structure-based offline fixtures also verify Wikimedia selection after
earlier providers return no usable result, including zero generated-provider
calls. This proves selection integration in tests, not a live image download or
WordPress publication. Next DEV verification needs this code update and process
restart, with the existing V2 provider list and all policies unchanged; no new
Wikimedia environment setting is required. Capture the correlated provider and
publication outcomes before claiming DEV provider acceptance.
