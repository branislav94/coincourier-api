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
