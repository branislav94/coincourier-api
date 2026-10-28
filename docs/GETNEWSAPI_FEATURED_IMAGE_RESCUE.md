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

The emergency pool includes strict-safe unattempted deliveries and soft
score/reuse rejections, in this order:

| Tier | Eligible candidate |
|---|---|
| 0 | Fresh delivery meeting its threshold, observed but not attempted by strict selection |
| 1 | Fresh below-threshold candidate |
| 2 | Above-threshold candidate blocked by local identity/WordPress recent use |
| 3 | Below-threshold candidate also blocked by recent identity/WordPress use |
| 4 | Exact SHA/difference-hash local content-history candidate |

Strict selection records each `(provider, usable_url)` in
`strict_attempted_transports` immediately before starting its download. Tier 0
requires no strict attempt and no score/history rejection, and passes the same
independent hard checks as every other rescue candidate. It preserves
`emergency_rescue_tier=0` and
`emergency_original_rejection_reason=strict_unattempted`; it is never labeled
below threshold. Thus a fresh safe candidate left behind by the strict global
budget is tried before lower rescue tiers or final generation, within the
separate emergency attempt budget. Existing URL-wide failure exclusions prevent
a failed URL from returning through another provider, while a different valid
delivery of the same asset remains eligible.

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
Additional fixtures cover a strict budget of zero and a consumed strict budget:
observed fresh stock meeting its threshold is rescued as Tier 0, uploaded and recorded
through the normal publication path, with no generated-image calls.

## Final generated-image phase

`IMAGE_REQUIRE_FEATURED_IMAGE=true` defaults on. For V2 hybrid/stock routing, once
the bounded emergency phase completes without featured media, the final chain
tries Grok and then the existing OpenAI image helper. Both existing image models
and preparation paths remain unchanged. This final requirement selects that
chain even when normal optional-generation settings would disable it; explicit
source/generated-only modes and V1 retain their existing routes.

The strict selector's `generation_allowed` and the meaning of
`IMAGE_GENERATION_ON_PROVIDER_ERROR=false` are unchanged: a provider failure still
blocks normal generation. The separate `final_generation_rescue_allowed` flag
can become true only after emergency completion, including an empty safe pool,
a configured zero attempt limit, or an explicitly disabled emergency phase.
Setting `IMAGE_REQUIRE_FEATURED_IMAGE=false` restores the normal generation gate
after emergency stock. Failed stock metadata never becomes featured media;
remaining candidates are tried before this final policy is considered.

A V2 stock-route image can still be absent if safe stock fails and both generated
providers fail/unavailable, or WordPress media upload fails. This produces an
ERROR `image_terminal reason=final_image_failure`, with `final_image_failure=true`.
Final source and strict/emergency/final-generation counters accompany success
and failure. Publishing semantics remain unchanged; this change does not add a
post-publication veto or alter Yoast handling. V1 rollback retains its existing
fail-closed metadata behavior, including possible publication without media.

## Next DEV verification

These are operator instructions only; this checkout has not changed runtime
configuration, deployed code, contacted DEV, or published a post. Add explicitly:

```dotenv
IMAGE_EMERGENCY_STOCK_RESCUE_ENABLED=true
IMAGE_EMERGENCY_STOCK_RESCUE_LIMIT=10
IMAGE_REQUIRE_FEATURED_IMAGE=true
```

Retain `IMAGE_SEARCH_ENGINE=v2`, provider order
`pexels,pixabay,openverse,wikimedia`,
`IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED=true`, and
`IMAGE_GENERATION_ON_PROVIDER_ERROR=false`, with all current thresholds, models,
dimensions, license rules and reuse settings. Restart through the existing DEV
release process after the approved code/config update. Capture the correlated
strict provider summaries, `[IMG-RESCUE]` outcomes, `[IMG-V2] image_terminal` and
post's actual `featured_media`, complete credits and usage recording. Generation
without emergency completion is a failure; generation after completed emergency
is the intended final policy. A successful safe-stock rescue must make zero
generated-image calls.

Openverse identification and Bearer checks remain unverified from the same DEV
egress. Use the opt-in procedure in
[provider connectivity](GETNEWSAPI_IMAGE_PROVIDER_CONNECTIVITY.md); no runtime
header change or third Openverse fix commit is justified by offline fixtures.
