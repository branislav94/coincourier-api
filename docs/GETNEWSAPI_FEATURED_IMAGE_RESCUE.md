# Featured-image rescue

## Emergency stock phase

V2 hybrid/stock image routing first performs the existing strict stock search.
Provider precedence, scores, thresholds, dimensions, license allowlist, queries
and the 20-day reuse policy are unchanged. A separate emergency phase runs only
when strict selection cannot supply usable featured media. V1 and explicit
source-only/generated-only routing retain their existing behavior.

```dotenv
IMAGE_EMERGENCY_STOCK_RESCUE_ENABLED=true
IMAGE_EMERGENCY_STOCK_RESCUE_LIMIT=10
```

The phase uses full candidates already observed during strict search, never logs
or a new provider search. Every candidate independently passes all legal,
attribution, public/provider-specific URL, metadata, MIME, size, dimensions and
orientation checks, even if strict relevance scoring rejected it before those
checks. Required attribution remains complete and bounded, including applicable
CC BY modification notices. Unsupported or uncertain licenses are not relaxed.

Only soft score/reuse rejections enter the emergency pool, in this order:

1. Fresh below-threshold candidates.
2. Above-threshold candidates blocked by local identity/WordPress recent use.
3. Below-threshold candidates also blocked by recent identity/WordPress use.
4. Exact SHA/difference-hash local content-history candidates.

Within each tier, raw final score sorts descending, followed by configured
provider precedence and the existing deterministic candidate tie-break fields.
The original score, threshold, source, license, attribution and rejection reasons
are retained. Emergency selection is marked separately; it is not ordinary
threshold acceptance and does not change the configured thresholds/reuse window.

Hard rejections never enter rescue: malformed/invalid required fields, untrusted
URLs or Commons identities, unsupported formats, oversize/undersize/portrait
images, unsafe/incomplete/oversize attribution and license failures. Download,
conversion or attribution-persistence failures permanently exclude that exact
delivery from rescue. Another valid delivery of the same identity can remain
eligible. Known failed URLs are not retried through another provider in rescue.

The separate limit bounds emergency candidate download/conversion-validation
attempts. Previously validated downloaded bytes can be reused without another
network download. Actual downloads still use the common downloader, retry policy,
byte inspection, SHA/difference hashes and existing crop/resize validation. Newly
discovered content-history matches belong to the last tier. Each attempted
delivery is logged; bounded failures advance to the next safe candidate.

The publisher validates upload and required attribution inside candidate
selection. An attribution update failure can therefore advance to another stock
candidate, without selecting uncredited media or immediately generating an image.
Successful rescue uses the ordinary WordPress media and post-success usage
recording paths, with emergency provenance preserved in metadata.

The image terminal record includes strict selection, rescue availability,
download attempts, selected provider/original rejection, final source and final
failure. Strict provider summaries remain separate from rescue counters. No
response bodies, article bodies, credentials or image bytes are logged.

Local fixture verification reproduces Pexels below-threshold safe candidates,
Pixabay image HTTP 429 failures, Openverse edge denial and no usable Wikimedia
result. The safe Pexels rescue supplies featured media and prevents generation.
This is offline evidence, not a new DEV publication or deployment.
