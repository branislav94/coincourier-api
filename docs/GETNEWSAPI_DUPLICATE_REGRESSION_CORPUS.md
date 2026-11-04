# Offline duplicate regression corpus foundation

This foundation evaluates the existing deterministic policy and enforcement through explicit in-memory repositories. It does not change article selection, duplicate thresholds, aliases, confirmation rules, scheduling, targets, publication state, providers, or daily-cap configuration. It has **zero independently adjudicated historical RAW pairs** and cannot establish historical accuracy or production readiness.

## Populations and evidence boundaries

The versioned fixture is `GetNewsAPI/tests/fixtures/g1a_duplicate_regression.json`.

| Population | Entries | Authority and limits |
|---|---:|---|
| Synthetic contract examples | S01–S09 | Explicitly fictional source-shaped inputs, with root second review of exact texts and semantic references before engine execution. This is automated contract review, not human historical ground truth. |
| Existing reconstructed baseline | R01 | Copies the existing `fomc_source_pair` reconstruction. Its historical-looking IDs are fixture identities, not immutable RAW captures. It is not a new holdout. |
| Operator FINAL minimal analogues | F01–F08 | Minimal paraphrases of operator-reviewed published event families; root reviewed these texts before execution. Synthetic identities/URLs and unknown event IDs/source dates are explicit. These are not the original pipeline RAW inputs. |
| Quarantined production observation | Q01 | RAW IDs 34778/34388 were observed as `related_event`. Complete immutable RAW/research/human labels were not retained. Inputs and semantic reference remain null; the observation cannot become duplicate gold. |
| Quarantined material exception | Q02 | Operator identifies FINAL WP6344 as a useful measured-finality update, excluded from the Alpenglow duplicate family. Concrete primary measurement evidence is not available here. It stays review-only, with no invented metric or RAW input. |

F06 preserves the operator's distinction between UK-Qian ownership-law proceedings and the Tianjin confiscation filing. The minimal facts do not establish exact `related_event` versus `broad_topic_overlap` granularity. Its reference is `insufficient_evidence`, with the narrower known constraint `duplicate_truth=false`; exact native-class comparison abstains.

The operator's reviewed FINAL family registry retains the approved Glamsterdam/Hoodi, Alpenglow, Deka/Sparkassen, JPMorgan/Solana, UK-Qian, separate Tianjin, Bitmine and Aviva groups. The registry is not a set of RAW-pair labels. F07's 6.05 million ETH/$15 billion figures come from retained FINAL titles WP6293/6498. F08's £50 million comes from the retained title WP6483. These figures are not invented, but they do not supply missing historical pipeline inputs.

No reviewed semantic label is derived from the engine's result. Semantic references remain fixed when observations disagree. `unrelated` explicitly maps to native `broad_topic_overlap`, because production has five classes and the reference vocabulary additionally distinguishes unrelated cases. `insufficient_evidence` has no native-class mapping.

## Frozen provenance

The owner-attested frozen FINAL SQLite file was opened only with standard-library SQLite, `mode=ro&immutable=1`, `query_only=ON`, a read transaction and rollback. Nonempty WAL/journal sidecars were refused. Before/after SHA-256 matched:

- `acceptance-2026-10-28.sqlite3`: `908f583389c5c5db28e7dd4c33fcf3d30c9643858b306f3318443df96c9636dd`
- Retained Markdown report: `a4cefdeece63514a15d4e82b6a02043eebb0c4e9d8059f895634796b3c06b135`
- Retained JSONL report: `adf2b3844acc4e3624f6d756376201d2f77682ab8924d96aa82a027c9df7a1ac`

Inspection was bounded to the 30 operator-approved public WP IDs and metadata/public titles. No FINAL bodies, payloads or private user data were exported into this fixture. The separate independent-label JSON was not available at a confirmed local path and has not been reconstructed from memory. Operator attestation of FINAL families is recorded separately from that missing artifact and from missing RAW evidence.

The older retained acceptance database has `article_snapshots`, `wp_posts`, `reporting_days`, collection tables and observations; it lacks the newer `report_revisions`/`report_articles` tables expected by an earlier operator-request helper. That helper was not executed or modified to force compatibility. Latest revision-1 published snapshots for the approved IDs were inspected; this alone does not prove a report-membership replay or original pipeline input provenance.

Each runnable case freezes complete prior/current fixture objects, original JSON primitive types, nulls, title case and strings, plus SHA-256 of canonical UTF-8 JSON (`ensure_ascii=False`, sorted keys, compact separators). Missing event identity and publication dates are preserved as null. The fixture records baseline commit/source hashes and explicit duplicate lookback/policy version. No protected absolute evidence path or application credentials are needed by tests.

## Evaluation layers

`tests/duplicate_regression_support.py` loads and validates reviewed, fingerprinted inputs. It invokes the real `assess_relationship`, confirmation predicate, `assess_duplicate_enforcement`, and prepublication `PublicationDuplicateGuard`. Source reloads, candidate rows and audit writes use an explicit fake repository. The publication mutex is an explicit fake context manager.

The report distinguishes:

1. Semantic reference and native classification, including an explicit taxonomy mapping or abstention.
2. Confirmation and its facts/uncertainty/contradictions, compared separately with pre-engine reviewed **contract confirmation** and with semantic duplicate truth. `contract_confirmation_mismatch` measures disagreement with the reviewed existing contract; `semantic_confirmation_alignment_gap` measures the different question of semantic alignment. A deliberate conservative allowance can have the second gap without a contract error.
3. Candidate eligibility and **returned assessment evidence**, mode, stage, availability, selected canonical, pair audit decision and final article decision. `assessment_evidence_observed=false` does not prove that no assessment ran: persistence failure can discard the returned audit. In that unavailable case eligibility/confirmation remain unknown, not confirmed negatives.
4. Audit persistence versus unavailable/incomplete evidence.
5. Disposition **intent**, with `executed=false`, `ownership_verified=false` and zero database state changes.

Raw rejection would request raw duplicate disposition; prepublication rejection would request rich duplicate disposition. An unresolved prior external attempt defers reconciliation instead of marking a terminal duplicate. This evaluator does not execute repository SQL, verify claim ownership, prove transaction atomicity, create an external post, or replace existing repository tests.

Sixteen stage scenarios cover raw/prepublish, shadow/off/enforce, published and processed-unpublished canonicals, selected-only/terminal/unattempted peers, unresolved attempts and audit failure/incomplete writes. Additional tests check source reload and published-canonical precedence independently of candidate order.

Runtime I/O guards reject database connections (including SQLite), HTTP/socket work and subprocess creation during evaluation. Dotenv loading is disabled and safe test environment values are scoped to the evaluation. Only duplicate version/lookback fields are temporarily patched and restored. No pipeline entry point or provider client is invoked.

## Reports and meaningful checks

The JSON report is deterministic: fixed cases/scenarios/settings, sorted serialization, input and runtime source hashes, and no wall-clock generation timestamp. Tests require identical bytes and report SHA-256 across repeated evaluation while leaving inputs unchanged.

Reports deliberately contain **no accuracy or precision score**. Independent historical eligibility stays zero; quarantines are excluded. Semantic/native-class and semantic/confirmation alignment gaps are visible observations, not rewritten labels or production error rates. They are distinct from actual-versus-reviewed-contract confirmation mismatches. Before any engine run, root reviewed contract confirmation as true for S01/S02/R01 and false for S03–S09/F01–F08. Sparse FINAL analogues may intentionally fail conservative confirmation. Their semantic gaps cannot prove that historical production RAW pairs were classified incorrectly.

Tests assert business-layer invariants rather than freezing every observed output as a passing golden result: shadow/off cannot reject; terminal rejection needs confirmation, availability, canonical identity and complete audit; protected update/distinct-event controls remain allowed; ineligible/unavailable cases are not silently treated as confirmed negatives; and raw/prepublication eligibility and deferral remain distinct. No accuracy floor is applied to minimal analogues.

Run only through the approved pinned, network-disabled test container after corpus review:

```text
python -m unittest GetNewsAPI.tests.test_g1a_duplicate_regression
```

The test package must be importable with the repository's existing isolated test bootstrap. This is a test command, not a production task, CLI, replay job or database command. Evaluation returns data in memory; callers may explicitly save its rendered report in an approved artifact directory. Tests do not write a report into the repository.

## Historical readiness still blocked

Actual historical quality evaluation requires immutable original RAW inputs and their decision-time types, directed pair chronology, source completeness, frozen candidate/canonical states, independently reviewed pair labels with rationale and abstentions, and exact code/settings identity. FINAL publication families, latest mutable rows, DEV captures and production classifications cannot substitute for those inputs. Source acquisition and adjudication require a separately approved bounded evidence step.

No duplicate product change is justified solely by these foundation cases. An independently established defect would need frozen relevant evidence, an independently supported reference, a reproducible baseline mismatch and an explanation of the responsible decision layer.
