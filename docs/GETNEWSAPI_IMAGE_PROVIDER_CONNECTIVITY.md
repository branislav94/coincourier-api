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
