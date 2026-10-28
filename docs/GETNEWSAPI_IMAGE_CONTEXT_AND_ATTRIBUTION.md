# Image context and public attribution

## Deterministic context guard

The previous token scorer could give an unrelated Commons image a passing score
when a project name also had a geographic or ordinary meaning. For the reported
JPMorgan/Solana article, a `solana blockchain` query and the brass-quartet/Solana
Beach fixture score 0.7225 against the unchanged Wikimedia threshold of 0.70:
the shared `solana` token earns both crypto relevance and topic bonuses.

V2 now checks ambiguous project context after normalization and before threshold
eligibility. It reuses the existing article/token normalization and a project
name/alias vocabulary. The article and query establish the intended subject;
the query is never evidence that a returned image depicts that subject.

Candidate evidence comes from normalized title, description, tags and other
subject metadata. Attribution HTML, creator names and URL targets do not provide
crypto corroboration. A misleading filename cannot defeat richer, genuine crypto
subject metadata. A shared ambiguous name needs corroborating crypto/finance
context, a clear project brand/ticker, or another meaningful article/query
concept. Unrelated beach/music, geometry or snow context does not gain crypto
meaning merely from a generic word such as `network` or `market`. A sparse
ambiguous-name-only result is rejected conservatively.

Failures use the explicit `context_mismatch` provider-summary reason, separately
from `score_below_threshold`. The rejection is hard: strict selection cannot
download it, and the rescue pool and rescue hard checks enforce the same context
decision. Tier 0–4 and mandatory generation cannot resurrect it. Legitimate
Solana blockchain/logo/token, Polygon/MATIC, Avalanche crypto and NEAR Protocol
fixtures remain eligible under the existing scoring and legal rules.

No model call, threshold change, query change or new configuration is introduced.
Provider precedence, rescue tiers/budgets, licenses, dimensions, reuse and final
Grok/OpenAI policy remain unchanged. V1 and explicit source/generated-only routes
retain their existing behavior. Verification is offline; no live selection or
deployment is implied by the fixtures.

## Compact public caption, complete stored attribution

The verbose block came from `_upload_prepared_image`: it sent the entire
provider credit plus the CC BY change notice to both WordPress attachment
`caption` and `description`. Required media metadata is deliberately not clipped,
so the caption contained every raw File/Artist/Credit/Source/License field. No
credit is injected into the article body.

The first compact-caption implementation generated `<a href="...">` markup.
The supplied DEV evidence shows those tags displayed literally. This repository
contains the REST writer, but no CoinCourier article renderer: no WordPress
theme/PHP templates, React/Next components, or media-caption consumer. The only
tracked JavaScript-related code is an unrelated Apps Script PDF receiver and its
test. The backend posts the caption unchanged and checks HTTP success; it does
not read back `caption.raw`/`caption.rendered`. Consequently this checkout cannot
identify whether the downstream text conversion occurs in WordPress, a theme,
React or another service. No external renderer change is inferred or attempted.

The publisher now sends a single **plain-text** caption for Wikimedia/Openverse CC
assets, built from the actual structured work title, creator, source, license
version and required notices. A typical visible result is:

> Photo: Paul Williamson · “Solana blockchain illustration” · Wikimedia Commons · CC BY 2.0 · Cropped

The caption string itself contains the human-readable creator, supplied title,
provider label, exact license name/version and crop indicator, plus required
plain-text notices. It requires no HTML interpretation. It contains no tags,
`href`/`rel` attributes, raw HTTP URLs, raw audit field labels or internal identity
keys. Only exact `Own work` or duplicate creator/title boilerplate is omitted.
CC BY custom credit that cannot be represented completely in this compact text
fails closed; a required additional credit URI is not silently discarded.

The full provider original remains in `provider_attribution_text`. Full
`credit_text` and `attribution_text` retain that original plus the existing CC BY
cropping/resizing notice, and still follow the existing usage/persistence path.
The attachment `description` retains the complete audit text in safely encoded
HTML, including the validated source/license URLs and supplied credit details.
Any missing structural attribution facts are added to the description rather
than to the public caption; the original provider metadata stays unchanged.
Existing escaped Commons text is not double escaped, and raw provider HTML cannot
execute. The caption uses normalized readable text, including real Unicode
punctuation and ampersands rather than HTML entities. Encoded HTML/script input
is never reactivated as caption markup.

Supplied titles are retained for every CC BY version, including 2.0, 2.5 and 3.0.
This follows the title/credit conditions in CC BY
[2.0 section 4(b)](https://creativecommons.org/licenses/by/2.0/legalcode.en),
[2.5 section 4(b)](https://creativecommons.org/licenses/by/2.5/legalcode.en), and
[3.0 sections 4(b) and 3(b)](https://creativecommons.org/licenses/by/3.0/legalcode).
Supplied notices and modification information also remain intact for
[4.0 section 3(a)](https://creativecommons.org/licenses/by/4.0/legalcode.en).
The [CC attribution guide](https://wiki.creativecommons.org/wiki/Best_practices_for_attribution)
explains the version distinction; the formatter consistently retains supplied
titles rather than dropping them for 4.0.

Existing complete-credit validation and its 16,384-character bound remain.
Required CC BY caption content is never truncated: an unsafe, incomplete or
unrepresentable credit fails closed and selection may try another candidate.
Each custom notice/party is bounded to 240 characters, and the complete visible
caption to 512. CC0/PDM optional display fields remain best-effort, with the full
audit retained. Pexels/Pixabay keep their existing public credit behavior, and
generated images acquire no CC caption or false license notice.

There is no theme/CSS change, WordPress migration, post-body change or new
environment setting. Featured-media assignment, title/slug/status/scheduling,
Yoast, identity and reuse recording retain their existing contracts. These are
local code/test changes only; no push, deployment or live metadata rewrite is
authorized or performed.

## Historical media and future clickable credits

This change affects newly prepared media only. Historical WordPress attachments
retain their existing raw-audit or HTML-anchor captions. There is no automatic
backfill, cache reset or migration. Historical cleanup needs a separately
controlled WP Admin operation or one-time backfill.

Clickable attribution would require work in the actual frontend repository after
identifying its media-field contract. Prefer exposing validated structured credit
fields and constructing safe, framework-escaped links. If HTML is used, the
platform's approved sanitizer must allow only the necessary safe anchors and
attributes. Never render arbitrary audit HTML or parse an audit string into a
new public data contract. The current release depends on neither that frontend
change nor an HTML-capable caption renderer.
