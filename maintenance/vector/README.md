# Local MariaDB vector service

This compose file is a reproducible local-only MariaDB 11.8 service. It binds
to `127.0.0.1:13309`, uses non-production defaults, and stores data in a named
local volume. It is independent from both canonical remote API Compose files.

**LOCAL DEVELOPMENT / VECTOR VALIDATION ONLY.**
**NOT FOR persistent Dokploy DEV, production, or remote shared infrastructure.** Its
local root/password defaults are disposable validation values, never remote
deployment credentials. Remote `docker-compose.dev.yml` and `docker-compose.yml`
create no vector service; their API consumes the existing managed `VECTOR_DB_*`
endpoint and does not own database lifecycle.

Start and inspect:

```powershell
docker compose -f maintenance/vector/docker-compose.vector.yml up -d
docker compose -f maintenance/vector/docker-compose.vector.yml ps
```

Stop while retaining local data, or remove the service and volume completely:

```powershell
docker compose -f maintenance/vector/docker-compose.vector.yml down
docker compose -f maintenance/vector/docker-compose.vector.yml down -v
```

Override the `VECTOR_MARIADB_*` compose variables for another disposable local
database. Application connection variables are the separate `VECTOR_DB_*`
settings documented in `.env.example`; `VECTOR_ENABLED` remains false.

The opt-in integration suite additionally requires the guarded database name
`coincourier_vectors_test`. For a fresh test service, set the matching disposable
values before startup:

```powershell
$env:VECTOR_MARIADB_DATABASE = "coincourier_vectors_test"
$env:VECTOR_MARIADB_USER = "vector_test"
$env:VECTOR_MARIADB_PASSWORD = "vector_test_only"
docker compose -f maintenance/vector/docker-compose.vector.yml up -d
$env:RUN_VECTOR_MARIADB_INTEGRATION = "true"
python -m unittest GetNewsAPI.tests.integration.test_vector_mariadb
```

The suite refuses non-loopback hosts and all other database names.

## Separately managed remote vector resources

Remote DEV and production use separate Dokploy-managed MariaDB 11.8 resources,
private internal port 3306, no public DB port, persistent DB storage, and reviewed
backup/restore ownership. Their API service must reach the managed endpoint on
the approved private Dokploy route; canonical API Compose uses external
`dokploy-network`. Hostnames and TLS policy are runtime `VECTOR_DB_*` values,
never guessed local-helper service names.

The user reports the current DEV vector resource is populated and its migrations
verified. Preserve that resource and data when replacing the API Application
with Compose. Do not start this helper as a replacement, copy local defaults into
remote secrets, or reapply schema as a cutover shortcut. Database provisioning
and new-environment migration work are separate approved operator tasks in the
[deployment runbook](../../docs/GETNEWSAPI_DEPLOYMENT_RUNBOOK.md).
`VECTOR_MARIADB_*` and database root credentials stay outside API runtime; only
`VECTOR_DB_*` application-user connection settings enter it. This repository
task performs no remote login, provisioning, migration, or data operation.
