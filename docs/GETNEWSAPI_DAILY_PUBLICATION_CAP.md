# Daily WordPress publication cap: Phase 1

The optional cap limits first WordPress CREATE authorization to 30 articles per
UTC publication date, across breaking stories, carryover, CLI jobs, and the
authenticated publish API. It is disabled by default. This document is an
operator procedure for a separately approved rollout; implementation and local
tests do not establish which commit is deployed in production.

## Configuration and prerequisites

```text
PUBLISH_DAILY_HARD_CAP_ENABLED=false
PUBLISH_DAILY_HARD_CAP=30
```

The cap must be an integer from 1 through 4294967295. Invalid configuration is
rejected rather than silently disabling enforcement. Keep the flag false until
the schema and activation baseline have been verified. Offline `config_check`
validates configuration; it does not establish that database tables exist.

All publishers targeting the same WordPress site must use the same reviewed
code, application database, enabled flag, and cap. Independent databases,
older images, disabled instances, external WordPress writers, and direct manual
WordPress creation cannot be constrained by this application's capacity store.
Different cap values within a UTC day fail closed against its saved guard.
Do not change the cap midway through a day without stopping all publishers and
reviewing existing reservations.

Retain existing durable-claim, WordPress reconciliation, lock, image, and SEO
settings. The cap also guards the legacy publishing path; enabling it does not
enable or disable either durable-state flag. Keeping the already deployed durable
safeguards enabled is recommended.

## Additive migration

Apply `maintenance/migrations/008_publication_daily_capacity.sql` to the existing
application database only after a backup, restore check, schema review, and
publisher quiescence. No application startup runs this migration.

The manifest identifies it as `app-008`, depending on `app-004`. Duplicate-shadow
and vector migrations are not prerequisites for capacity reservations. The
general application migration tool still inventories its existing full chain;
review its plan rather than assuming it selects only this file.

The migration creates two InnoDB tables:

| Table | Purpose |
|---|---|
| `publication_capacity_days` | One UTC date guard, fixed cap, and verified activation baseline. |
| `publication_capacity_reservations` | One stable rich article/publication identity, reserved date, CREATE marker, supplied publication timestamp, and confirmed WordPress ID. |

Schema verification checks transactional engines, column contracts, primary
keys, unique publication identity, and the date index. Existing objects with
these names must match the contract; `IF NOT EXISTS` does not repair an
incompatible preexisting table. Retain both tables on rollback.

These read-only checks are examples for an authorized operator using the
reviewed image and intended environment. They were not run against production:

```text
python tasks.py config_check publish
python tasks.py migration_plan app
python tasks.py migration_check app
python tasks.py migration_verify app
python tasks.py feature_readiness
```

`daily_publication_cap=ready` establishes migration/schema/configuration
readiness, not correct historical baseline accounting or coordinated activation.
The readiness command does not enable flags. `migration_apply` is a mutating
operator step and requires the existing deployment runbook's approval and backup
procedure. Use the guarded migration tooling after reviewing its plan. If the
plan includes unrelated pending migrations, stop and agree their rollout scope
first. Existing tables without verified ledger history are `untracked_applied`;
the current tooling intentionally has no adoption operation. Do not precreate
managed tables or fabricate ledger records to bypass that stop condition.

## Preferred activation at a controlled UTC day boundary

1. Record the immutable image digest/deployed SHA and inventory every scheduled,
   manual, CLI, API, and old deployment publication entry point. Confirm that
   every publisher for the site shares the application database and cap.
2. Disable all recurrence and block new publish requests. Stop or drain every
   publication process, including direct scripts and old containers. A stopped
   scheduler alone does not stop an already running CREATE.
3. Resolve earlier pending CREATE operations and ambiguous outcomes using
   WordPress IDs, application mappings, request logs, and WordPress records.
   Reconcile posts already created even when application `published=0` because
   SEO failed. Do not reset claims, attempt counters, or missing-ID errors to
   authorize another CREATE. Unresolved earlier no-ID attempts must remain held
   for manual review, including if legacy publishing was used.
4. Apply and verify the additive migration with the cap disabled. Deploy the
   reviewed image everywhere while publication remains stopped. Verify no old
   image or independent producer can publish to the same site.
5. Remain quiescent across the selected UTC midnight. Confirm there are no
   pre-activation CoinCourier posts or unresolved CREATE authorizations with the
   new day's WordPress `date_gmt`. If any exist, use verified initialization
   below rather than treating the day as empty.
6. Enable `PUBLISH_DAILY_HARD_CAP_ENABLED=true` with cap 30 consistently on every
   instance, retaining existing durable flags. Complete configuration and schema
   checks before resuming publication. The first capacity reservation creates
   that day's guard with a zero baseline only after the empty-day condition has
   been established operationally.
7. Resume the approved jobs and API entry points. Verify reservation rows and
   WordPress IDs, inspect quota deferrals, and confirm a full day cannot authorize
   an extra CREATE. Observe the next UTC rollover before closing rollout.

UTC midnight is the accounting boundary regardless of the WordPress site or
operator's local timezone. Do not substitute a site-local date filter.

## Verified initialization when activating during a UTC day

Midday activation with an empty capacity table would allow 30 additional posts
on top of earlier posts. Prefer the boundary procedure. If midday activation is
required, keep every publisher stopped throughout reconciliation and initialization:

1. Export all relevant WordPress posts with IDs, statuses, and `date_gmt`; use
   complete pagination and exact half-open UTC bounds `[D 00:00Z, D+1 00:00Z)`.
2. Attribute distinct CoinCourier posts using application-to-WordPress mappings
   and retained evidence. Include successful CREATEs awaiting SEO completion.
   Do not count application `published_at`, selected sources, or successful
   application completions as a proxy for WordPress creation.
3. Resolve pending requests. Where an ambiguous CREATE cannot be ruled out,
   conservatively account for its possible slot and keep its identity in manual
   review; no process may issue another CREATE for it. Unknown attribution or
   unresolved dates prevent a trustworthy initialization, so remain stopped.
4. Initialize the day guard's `baseline_count` to verified pre-activation posts
   plus conservatively held ambiguous slots **not already represented by that
   day's reservation rows**. Avoid counting an identity in both baseline and
   reservations. Retain the post-ID list and evidence for the baseline.
5. An authorized operator can initialize a genuinely absent day guard using a
   parameterized INSERT against the reviewed database:

   ```sql
   INSERT INTO publication_capacity_days (`utc_date`, hard_cap, baseline_count)
   VALUES (%s, %s, %s);
   ```

   Supply the verified UTC date, cap 30, and verified baseline separately from
   SQL text. A duplicate-key error requires reconciliation of the existing
   guard; do not use an upsert that overwrites accounting. Re-read the guard and
   reservations before enabling publishers. Baseline plus reservations at 30
   leaves no capacity; a total above 30 fails closed for the remainder of that
   day and cannot undo earlier excess publication.
6. Complete coordinated enablement and resumption as above. Existing WordPress
   IDs must remain attached so their metadata retries reconcile without CREATE.

This is a manual, reviewed initialization procedure, not an automatic backfill.
Do not insert synthetic reservation identities or decrease the baseline simply
to restore throughput.

## Runtime guarantees and expected behavior

Capacity admission locks the UTC day guard and identity rows in short database
transactions. Concurrent workers cannot share the last slot. Reservations are
bound to the rich article ID and stable publication key; retries reuse an
unattempted reservation rather than consuming another slot. No provider or HTTP
request runs inside a capacity transaction.

Before images, a new article reserves a slot. Immediately before CREATE, a
second transaction commits an irreversible attempt marker and the UTC timestamp
sent as WordPress `date_gmt`. If work crosses midnight before that transaction,
the unattempted reservation moves only if the new day has capacity. An attempt
already marked remains charged to its original date and can only reconcile.
The publication date supplied to WordPress remains the persisted date even if
the HTTP request completes after midnight. The cap therefore counts the UTC
`date_gmt` supplied to WordPress, not the wall-clock instant a slow request first
makes a post visible. A request authorized just before midnight can become
visible after midnight while consuming the previous publication date's slot.

Confirmed CREATE, image failure, metadata failure, timeout, crash, or uncertain
HTTP outcome never automatically frees a reservation. A crash after the CREATE
marker but before the request can underuse a day; that is the deliberate safety
tradeoff. Existing WordPress-ID reconciliation consumes no new slot and remains
eligible when capacity is full. Never clear an attempted reservation to retry
CREATE merely because no application completion timestamp exists.

When capacity is exhausted, new posts wait with recognizable quota deferral
status/logs rather than being marked published or discarded. Breaking stories
receive priority within the available capacity. Capacity checks before selection,
generation, and image preparation avoid work that is clearly blocked. These
checks are advisory; only the atomic reservation authorizes CREATE, and work
already underway may still finish after another worker takes the last slot.

With the feature enabled, stale unpublished carryover beyond
`SCHEDULE_MAX_AGE_HOURS` (default 36), based on source publication time with
selection-time fallback, is retained for manual review with
`stale_backlog_review_required`. Missing or invalid freshness provenance also
requires review. Existing WordPress posts continue reconciliation.
No automatic editorial deduplication, source rewriting, or prompt/provider
change is introduced. Review stale items explicitly before rescheduling rather
than draining them blindly the following day.

Expected volume is at most 30 newly authorized CoinCourier posts per UTC
publication date for coordinated publishers, possibly fewer because reservations
are conservative. External WordPress writers and backdated/edited WordPress
dates remain outside that guarantee. Avoidable processing should decrease during
full-capacity periods; no numerical cost saving is claimed without usage data.

## Rollback

1. Stop new publication requests and all publisher processes first; drain or
   reconcile in-flight requests and preserve pending/ambiguous identities.
2. Prefer keeping the reviewed cap-enabled version with publishing stopped while
   diagnosing a problem. Disabling the feature restores unlimited legacy
   CREATE behavior and must not be treated as a quota-preserving rollback.
3. If disabling or reverting code is separately approved, keep new CREATEs
   blocked until an alternative operational stop is in place. Restore flags
   consistently everywhere; retain the existing durable-state flags and tables.
4. Keep capacity guards, reservations, WordPress pointers, and attempt markers.
   Do not truncate or decrement accounting. Before re-enabling, repeat
   reconciliation and either use a controlled UTC boundary or a verified baseline
   for posts created while enforcement was off.

   A capacity commit can succeed even when its acknowledgement is lost. A
   pre-HTTP quota deferral can then leave `publish_status='quota_deferred'` with
   zero application attempts while its capacity row contains a CREATE marker.
   The enabled ledger still blocks CREATE. Before resuming any disabled or older
   publisher, reconcile or hold every such identity in `manual_review`; do not
   treat zero application attempts as proof that its ledger marker can be cleared.

Local validation does not authorize production or DEV migrations, runtime flag
changes, restarts, deployments, or verification against retained service data.

## Implementation inventory and local verification

The original 28-file candidate was preserved before integration, including its
untracked files. It was integrated on `feature/daily-publication-capacity` onto
the fetched `origin/dev` commit
`e30ba209fe589d253aeba85265baf8c90c228729`, preserving its existing history.
This identifies the local integration base, not a deployed image. No push,
deployment, production/DEV migration, or service setting change was performed.

The integration retains upstream duplicate policy, processing outcomes and
claim-token checks, image attribution and rescue behavior, and the Yoast
whitespace-readback fix. Both publication callbacks remain active: the existing
final duplicate check precedes irreversible capacity authorization, creation
intent, and HTTP CREATE. The new `quota_deferred` state is accepted by the
existing ownership-checked legacy duplicate disposition. The candidate's
existing optional freshness handling is retained; no additional validator or
editorial publication target change was introduced.

Exact changed/new files:

```text
.env.example
GetNewsAPI/config.py
GetNewsAPI/deployment/migrations.py
GetNewsAPI/deployment/service.py
GetNewsAPI/deployment/verification.py
GetNewsAPI/fetcher.py
GetNewsAPI/gpt_processor.py
GetNewsAPI/operations/jobs.py
GetNewsAPI/publishing/capacity.py
GetNewsAPI/publishing/models.py
GetNewsAPI/publishing/service.py
GetNewsAPI/publishing/wordpress/publisher.py
GetNewsAPI/repositories/publication.py
GetNewsAPI/repositories/publication_capacity.py
GetNewsAPI/repositories/raw_news.py
GetNewsAPI/runtime/config_validation.py
GetNewsAPI/tests/environment_isolation.py
GetNewsAPI/tests/integration/test_migration_tooling_mariadb.py
GetNewsAPI/tests/integration/test_publication_capacity_mariadb.py
GetNewsAPI/tests/test_duplicate_publication_repository.py
GetNewsAPI/tests/test_migrations.py
GetNewsAPI/tests/test_publication_capacity.py
GetNewsAPI/tests/test_publication_capacity_admission.py
GetNewsAPI/tests/test_publication_capacity_config.py
GetNewsAPI/tests/test_publication_capacity_jobs.py
GetNewsAPI/tests/test_publication_capacity_publishing.py
GetNewsAPI/tests/test_yoast_metadata_sync.py
docs/GETNEWSAPI_DAILY_PUBLICATION_CAP.md
maintenance/migrations/008_publication_daily_capacity.sql
maintenance/migrations/README.md
```

Verification of the combined implementation on October 30, 2026:

- Python 3.11.18, with every direct and transitive dependency matching
  `GetNewsAPI/requirements.txt`. Validation used an existing pinned container
  image and a read-only snapshot containing no local secret files.
- Full GetNewsAPI offline discovery: 1,293 tests, 1,222 passed and 71 opt-in
  integration tests skipped; no failures or errors. Docker Compose contract
  tests ran using the CLI without daemon access. Dotenv loading was disabled,
  endpoints/settings were synthetic, and the container had no network.
- All 71 native integration tests passed separately: nine capacity, 24 durable
  state, three existing duplicate-enforcement, two job-lock, seven migration
  tooling, and 26 vector/embedding tests. MariaDB 10.4.34 and 11.8.9 ran in
  disposable containers with RAM-backed data, no host ports or retained data
  mounts, and a shared isolated loopback namespace without external egress.
  Each module received a fresh dedicated test schema to prevent fixture overlap.
- Capacity coverage includes final-slot and same-identity races, midnight
  transfer, schema failure, retained ambiguous CREATE intent, and native
  duplicate rejection after reservation but before CREATE authorization.
- Combined offline regressions cover enforcement/shadow counters, lost claim
  ownership, both duplicate-check boundaries, callback ordering, legacy quota
  disposition, and the candidate's stale-row precedence. The existing Yoast
  restart test bootstrap now passes interpreter dependency paths explicitly so
  its `-I -S -B` isolation works on Linux as well as Windows.
- No live WordPress request, paid provider call, production/DEV database access,
  or application pipeline was performed. `git diff --check` passed. These local
  results do not approve deployment or cap activation.

The full offline suite can be rerun from the repository root in a Python 3.11
environment with the pinned requirements already installed, using the
test-isolation bootstrap before discovery. Run with network access disabled:

```powershell
py -B -c "from GetNewsAPI.tests.environment_isolation import install_test_environment; install_test_environment(); import os,sys,pathlib,unittest; os.environ.update({'RUN_MARIADB_INTEGRATION':'false','RUN_VECTOR_MARIADB_INTEGRATION':'false','RUN_MIGRATION_TOOLING_INTEGRATION':'false','RUN_JOB_LOCK_INTEGRATION':'false'}); sys.path.insert(0,str(pathlib.Path('GetNewsAPI').resolve())); suite=unittest.defaultTestLoader.discover('GetNewsAPI/tests',top_level_dir='.'); result=unittest.TextTestRunner().run(suite); raise SystemExit(not result.wasSuccessful())"
```

For native capacity tests, use the existing disposable MariaDB instructions and
explicit loopback `MARIADB_TEST_*` settings, then run
`GetNewsAPI.tests.integration.test_publication_capacity_mariadb` with
`RUN_MARIADB_INTEGRATION=true`. These tests refuse non-loopback hosts and database
names without the `_test` suffix and use destructive synthetic fixtures; never
point them at retained or production data.

Run native modules against independently reset schemas. In particular, the
vector and embedding modules share a test database name but create different
foreign-key graphs; reset that disposable schema between modules rather than
reusing another module's retained tables.
