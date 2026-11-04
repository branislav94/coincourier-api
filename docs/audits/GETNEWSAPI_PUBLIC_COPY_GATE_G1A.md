# G1A public-copy shadow observations

This local foundation observes five phrases in article-body HTML: generation
time, reporting cutoff, supplied evidence, available evidence and reviewed
coverage. It makes no factual, source-support, novelty or publishing decision.
No phrase is mechanically removed or rewritten.

`PUBLIC_COPY_GATE_MODE` accepts `off` and `shadow`, with the configuration default
`shadow`. There is no enforcement mode. Off skips evaluation/logging; shadow
continues the existing processing/publishing outcome even when a phrase occurs,
HTML cannot be evaluated, logging fails or the observer unexpectedly raises.
Production activation/deployment is separate from this source change.

## Scope and interpretation

The pure `GetNewsAPI/public_copy_gate.py` module imports standard library only.
It evaluates only `full_text` / `PublicationArticle.html_content`. Titles,
descriptions, focus phrases, image attributes and JSON-LD are outside this first
detector's scope. A clear result is not editorial approval.

The HTML parser preserves inline text continuity, decodes entities once, keeps
block boundaries separate and excludes attributes/URLs, comments, scripts,
styles, noscript/template content and code/preformatted examples. It does not
inspect a browser DOM, CSS visibility or JavaScript rendering. The term matching
is case-insensitive, whitespace-tolerant and word-bounded; it does not add fuzzy,
hyphenated, obfuscated or semantic matches.

Each occurrence has one of three metadata labels:

| Label | Meaning |
| --- | --- |
| `workflow_reference_candidate` | The phrase shares a bounded clause with an explicit marker such as this prompt/draft, provided to this model/writer, or the generated article/response. It remains a review candidate, not a confirmed error. |
| `quoted_candidate` | The phrase appears in explicit HTML quotation markup or a balanced text quotation. This recognizes presentation, not authentic attribution. |
| `ambiguous_candidate` | Ordinary reporting, technical terminology or unclear context; also used after an unmatched opening quotation. No rejection is justified by this label. |

Ordinary uses such as key generation time, a company's reporting cutoff, a
regulator supplying evidence, available investigative evidence and a committee
reviewing coverage must not become hard failures. Quotation heuristics and
bounded clause matching can still misclassify context. All findings require
review before any later policy decision.

Inputs above 100,000 HTML characters or non-string inputs return an explicit
not-evaluated status. Findings stop at 64 with `truncated=true`; reported counts
then describe that bounded result set, not complete article prevalence. Malformed
HTML tolerated by Python's parser may still be partially evaluated; a parser
exception returns a not-evaluated status. An unavailable observation is not a
clean article.

## Pipeline boundaries

The generation observation runs after final source-href cleanup and existing
hard validation, immediately before storage. It does not add a hard validator,
soft repair trigger, provider request, expansion, fallback or processing retry.
The 450-word minimum and all existing writer/provider behavior remain intact.

The central WordPress observation runs only after existing-ID reconciliation has
returned no result, before preparing a new post payload. It covers ordinary new
CREATE calls from the legacy and durable publishing paths, including existing
rich backlog. Existing post metadata recovery short-circuits the observation;
it does not become a canonical-body update. Image work can already have happened
before this central call, and the observer does not authorize or block any work.

The observer emits only controlled stage/status/rule/category counts, validated
positive integer raw/rich IDs, truncation and `decision=continue`. It emits no
body, sentence, title, URL, source evidence, provider payload or exception message.
Unexpected observer/logging exceptions are fail-open. No database schema or
durable event table is added; log rows are not unique publication events, and
pre-storage/pre-create observations of the same item must not be summed as
distinct articles.

Daily-cap activation remains off for the authorized work. Migration 008, cap
behavior, duplicate policy, targets, batch limits and publication volume are not
changed by these hooks.

## Verification and evidence limits

The new detector tests use synthetic ordinary, workflow, quoted, ambiguous and
markup examples. They assert parser/metadata contracts and preservation, not
independent editorial accuracy. Mocked hook tests cover final cleanup placement,
unchanged body/payload/provider-call counts, hard-invalid output, observer
failure, and reconciliation without another CREATE. Existing 449/450 and
provider/repair tests remain relevant.

At authoring, these tests have not been executed: the root agent owns the
isolated Docker harness and will record its actual results. Static parsing does
not establish runtime success. No local host package installation or live
provider/database/WordPress request is part of this detector implementation.

No independently labelled historical corpus is created or claimed. A later
precision evaluation should label distinct workflow, legitimate, quoted and
ambiguous cases before tuning rules, preserve an independent holdout, and keep
formatting variants separate from independent stories. A retained phrase count,
DOM location or body hash cannot reconstruct a missing sentence or establish its
editorial label. Production shadow telemetry alone cannot establish recall,
false-positive rate or readiness for blocking.
