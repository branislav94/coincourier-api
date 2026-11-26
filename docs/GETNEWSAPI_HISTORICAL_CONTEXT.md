# Historical context for CoinCourier articles

`RAG_CONTEXT_MODE` defaults to `off`. It controls an optional editorial check
in article processing.

| Mode | Behavior |
| --- | --- |
| `off` | Skip the historical context check. |
| `shadow` | Evaluate the repaired draft and log the decision. The writer and stored draft follow the existing path. |
| `apply` | Append one dated, attributed sentence with its source citation to an eligible draft. An invalid optional paragraph is discarded. |

The check runs after duplicate screening and writer repair. It uses the existing
semantic shortlist and embedding version. It makes one bounded passage read when
there are eligible candidates. It requires an original source article published
strictly before the current source and verifies the exact matched chunk. For a
proposed sentence, a source-ID lookup verifies the current raw article's URL,
publication time, and normalized title and body hash against the indexed
document. A correction can invalidate a passage before its new embedding is
ready. A publication timestamp at or after ingestion can be the fetcher's
fallback for an undated report. The check skips those dates for both the
current and earlier source. A late embedding, failed read, or unverifiable
passage leaves article processing able to continue. The check uses zero
model calls.

The first policy covers a report that withdrawals resumed for a specific named
vault when a prior indexed report documents that vault's withdrawal pause and
the draft still lacks that background. The citation date labels the report.
Semantic distance orders candidates. The passage and the current development
establish the connection. The check also reads the surrounding passage to
distinguish a reported pause from an exercise scenario.

## DEV shadow pilot

Use this setting in the DEV runtime environment for the processing job.

```dotenv
RAG_CONTEXT_MODE=shadow
```

The existing `VECTOR_ENABLED=true` and `SEMANTIC_SHADOW_ENABLED=true` settings
supply the shortlist. Verify those settings before sampling. Keep the existing
retrieval window, candidate limit, embedding version and schedules. Run the
existing `python GetNewsAPI/tasks.py config_check process` command from the
repository root with the DEV runtime environment to
confirm that the mode is `shadow`.

1. Collect the first 50 consecutive distinct source article IDs in
   `[RAG-CONTEXT]` records with `event=generation_started`. Use the first
   generation attempt for each ID. Keep articles with missing embeddings and
   attempts that fail before a decision in the sample.
2. Count how many had `embeddings_ready=True` at generation start. Count
   suggestions separately using `event=decision` with `decision=would_apply`.
3. For each suggestion, load the matched passage using the logged document ID,
   chunk index and embedding version. Compare its ID and hash to the log before
   reconstructing the proposed sentence. Compare it with the generated draft
   and the current source. Record the editor's judgment as useful, redundant
   or wrong. Record missing or changed evidence as unreviewable.
4. Sample skipped articles across different reason codes, including articles
   with ready embeddings. Check whether a reliable earlier fact would have
   helped. Record missed opportunities and their reasons.
5. Report the full cohort size, embedding-ready count, suggestion count,
   useful/redundant/wrong counts, unreviewable and missing decisions, and the
   size and findings of the skip sample. Review the results before enabling
   `apply`.

Decision records include source and document IDs, passage identity, embedding
version, reason codes, and evaluation latency. Candidate skip records give the
source and document IDs, chunk index, and reason. Article bodies and passages stay out of
these logs.

## Rollback

Set the processing job's DEV runtime setting to the following and reload the
job environment.

```dotenv
RAG_CONTEXT_MODE=off
```

The setting takes effect on the next processing invocation. Published articles
or already stored drafts retain their current body.
