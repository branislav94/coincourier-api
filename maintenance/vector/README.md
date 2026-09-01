# Local MariaDB vector service

This compose file is a reproducible local-only MariaDB 11.8 service. It binds
to `127.0.0.1:13309`, uses non-production defaults, and stores data in a named
local volume. It is independent from the main application compose file.

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

## Future Dokploy reproduction

Do not execute these steps during Phase 6A. Later, create a service named
`vector-mariadb` using the same tested `mariadb:11.8` family, database
`coincourier_vectors`, internal port `3306`, no public port, and the same private
Dokploy network as GetNewsAPI. Attach persistent storage, configure scheduled
off-server backups, and inject production-specific credentials as secrets.

Provision an empty service, wait for health, create the database/user, apply the
exact reviewed vector migrations, configure GetNewsAPI vector credentials, and
keep `VECTOR_ENABLED=false`. Verify private connectivity and schema before any
later Phase 6B or Phase 6C enablement. No automated Dokploy login or deployment
script is provided here.
