# G1A editorial mode configuration

`DUPLICATE_ENFORCEMENT_MODE` is the only supported duplicate-mode variable.
Unset means `shadow`; explicit values are `off`, `shadow` or `enforce`
(surrounding whitespace/case are normalized). Explicit blank/invalid values
fail configuration. `DUPLICATE_ENFORCE_MODE` is rejected even when blank or
accompanied by the canonical variable. It is never an alias that can activate
enforcement. Errors name the field and allowed values without echoing the value.

The operator reported a production misspelling of both the variable name and
value. Previously the unknown name was ignored, allowing the canonical variable
to take its shadow default. This change makes that configuration fail preflight
and application config loading. Existing production configuration is untouched;
production enforcement is **not approved** by G1A. A future deployment needs its
own operator configuration review and release approval.

Before deploying G1A code or restarting any G1A process, remove
`DUPLICATE_ENFORCE_MODE` entirely from the target environment. Do not leave it
present with an empty value or alongside `DUPLICATE_ENFORCEMENT_MODE`: a blank
legacy key and legacy-plus-canonical both cause configuration failure. The old
key is unsupported, not a deprecated-but-supported alias.

For the DEV observation rollout, with semantic shadow already accepted, use
this operator checklist:

```text
REMOVE:
DUPLICATE_ENFORCE_MODE

SET:
DUPLICATE_ENFORCEMENT_MODE=shadow
PUBLIC_COPY_GATE_MODE=shadow

KEEP:
SEMANTIC_SHADOW_ENABLED=true
PUBLISH_DAILY_HARD_CAP_ENABLED=false
```

This checklist does not attest to current live values or bypass the runbook's
new-environment stages. Keep semantic shadow true only at its already accepted
DEV observation stage. Keep the daily cap disabled under its existing separate
activation guidance. If its variable is absent, record it as unset; do not call
that an explicit operator-verified false value.

Validate the target configuration before enabling scheduled or manual execution:

```text
python GetNewsAPI/tasks.py config_check web
python GetNewsAPI/tasks.py config_check pipeline
```

Validate any other intended job profiles as well. These preflight commands do
not run application jobs, and successful validation does not approve enforcement.

`PUBLIC_COPY_GATE_MODE` supports only `off` and `shadow`, default `shadow`.
`enforce` is invalid: G1A adds deterministic observations and synthetic contract
tests, not production suppression. It does not change article content, provider
selection, minimum length, repair/fallback, daily targets or scheduling.

`config.py` and offline runtime preflight use the same pure parser. The API
lifespan emits controlled values/source labels from loaded config. Operational
jobs emit a validated invocation-environment summary before work/locks; the log
explicitly identifies that scope. `tasks.py config_check` reports only validated
editorial modes and whether they were explicit or defaults, without importing
application configuration or opening a connection. Normal `tasks.py` CLI
bootstrap imports `runtime.logging_config`, which calls `load_dotenv()` before
the environment snapshot. The mapping can therefore contain both injected and
dotenv-loaded values. `source=environment` means the key is present in that
post-bootstrap mapping; it does not identify injection provenance. This view
does not inspect another process's imported constants.

No environment dump, secret values, URLs or database settings are included in
these summaries. In an existing long-lived process, changing injected settings
does not prove previously imported constants changed; no runtime mutation is
implemented. The daily-cap flag remains disabled/unset and migration 008 is
unchanged. This work does not change Dokploy or enable any production gate.
