from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from GetNewsAPI.tests.environment_isolation import isolated_subprocess_environment

import tasks
from runtime.config_validation import format_config_issues, validate_runtime_config
from runtime import logging_config


DOCKERIGNORE_PATH = REPOSITORY_DIR / ".dockerignore"
DOCKERFILE_PATH = REPOSITORY_DIR / "Dockerfile"
PRODUCTION_COMPOSE_PATH = REPOSITORY_DIR / "docker-compose.yml"
DEV_COMPOSE_PATH = REPOSITORY_DIR / "docker-compose.dev.yml"
ENV_EXAMPLE_PATH = REPOSITORY_DIR / ".env.example"
PROVISIONING_ENV_EXAMPLE_PATH = REPOSITORY_DIR / ".env.provisioning.example"
CONFIG_PATH = PROJECT_DIR / "config.py"
APP_PATH = PROJECT_DIR / "app.py"
REQUIREMENTS_PATH = PROJECT_DIR / "requirements.txt"
RUNBOOK_PATH = REPOSITORY_DIR / "docs" / "GETNEWSAPI_DEPLOYMENT_RUNBOOK.md"
PRODUCTION_ENVIRONMENT_PATH = (
    REPOSITORY_DIR / "docs" / "GETNEWSAPI_PRODUCTION_ENVIRONMENT.md"
)


def minimal_web_environment() -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "DB_USER": "application_user",
        "DB_PASSWORD": "database-secret-value",
        "DB_HOST": "database.internal",
        "DB_PORT": "3306",
        "DB_NAME": "coincourier",
        "PUBLISH_API_TOKEN": "publish-secret-value",
        "ENABLE_APSCHEDULER": "false",
        "PROCESS_DURABLE_CLAIMS_ENABLED": "false",
        "PUBLISH_DURABLE_STATE_ENABLED": "false",
        "DUPLICATE_SHADOW_ENABLED": "false",
        "VECTOR_ENABLED": "false",
        "EMBEDDING_ENABLED": "false",
        "SEMANTIC_SHADOW_ENABLED": "false",
        "FILE_LOGGING_ENABLED": "false",
    }


REMOTE_COMPOSE_CONTRACTS = (
    (DEV_COMPOSE_PATH, "getnewsapi-dev", "getnewsapi-dev-state"),
    (PRODUCTION_COMPOSE_PATH, "getnewsapi-prod", "getnewsapi-prod-state"),
)

DEPLOYMENT_INVARIANTS = {
    "APP_ENV": "production",
    "API_DOCS_ENABLED": "false",
    "ENABLE_APSCHEDULER": "false",
    "PYTHONUNBUFFERED": "1",
    "WRITABLE_STATE_DIR": "/data",
    "STOCK_IMAGE_CACHE_DIR": "/data/cache/stock_images",
    "STOCK_IMAGE_USAGE_PATH": "/data/stock_image_usage.json",
}

# Deliberately conflicting runtime values prove that Compose owns invariants
# while feature flags, endpoints, and credentials still come from the env file.
SYNTHETIC_RUNTIME_ENVIRONMENT = {
    **minimal_web_environment(),
    "APP_ENV": "development",
    "API_DOCS_ENABLED": "true",
    "ENABLE_APSCHEDULER": "true",
    "PYTHONUNBUFFERED": "0",
    "WRITABLE_STATE_DIR": "/app/cache",
    "STOCK_IMAGE_CACHE_DIR": "/app/cache/images",
    "STOCK_IMAGE_USAGE_PATH": "/app/cache/usage.json",
    "WP_API_URL": "https://wordpress.example.invalid",
    "WP_USERNAME": "synthetic_wordpress_user",
    "WP_APP_PASSWORD": "synthetic-wordpress-password",
    "WP_DB_HOST": "wordpress-db.example.invalid",
    "WP_DB_PORT": "3306",
    "WP_DB_NAME": "wordpress_synthetic",
    "WP_DB_USER": "synthetic_wordpress_db_user",
    "WP_DB_PASSWORD": "synthetic-wordpress-db-password",
    "VECTOR_DB_HOST": "vector-db.example.invalid",
    "VECTOR_DB_PORT": "3306",
    "VECTOR_DB_NAME": "vectors_synthetic",
    "VECTOR_DB_USER": "synthetic_vector_user",
    "VECTOR_DB_PASSWORD": "synthetic-vector-password",
    "VECTOR_DB_CONNECT_TIMEOUT_SECONDS": "7",
    "VECTOR_DB_SSL_ENABLED": "false",
    "VECTOR_DB_SSL_VERIFY_CERT": "false",
    "VECTOR_DB_SSL_VERIFY_IDENTITY": "false",
    "VECTOR_DB_SSL_CA": "",
    "CRYPTO_NEWS_TOKEN": "synthetic-cryptonews-token",
    "OPENAI_API_KEY": "synthetic-openai-key",
    "GROK_API_KEY": "synthetic-grok-key",
    "PEXELS_API_KEY": "synthetic-pexels-key",
    "PIXABAY_API_KEY": "synthetic-pixabay-key",
    "PROCESS_DURABLE_CLAIMS_ENABLED": "true",
    "PUBLISH_DURABLE_STATE_ENABLED": "true",
    "DUPLICATE_SHADOW_ENABLED": "true",
    "VECTOR_ENABLED": "true",
    "EMBEDDING_ENABLED": "true",
    "SEMANTIC_SHADOW_ENABLED": "true",
}


def compose_subprocess_environment(overrides=None) -> dict[str, str]:
    environment = isolated_subprocess_environment(overrides)
    # Docker's Windows plugin search uses these installation paths. Do not
    # inherit parent credentials or Docker connection/configuration settings.
    environment.update(
        {
            name: value
            for name, value in os.environ.items()
            if name.upper() in {"PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"}
        }
    )
    return environment


def compose_cli() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise unittest.SkipTest("Docker CLI is not installed")
    version = subprocess.run(
        [executable, "compose", "version"],
        cwd=REPOSITORY_DIR,
        env=compose_subprocess_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if version.returncode != 0:
        raise unittest.SkipTest("Docker Compose is not available")
    return executable


def resolved_compose(
    executable: str,
    compose_path: Path,
    *,
    runtime_environment: dict[str, str] | None = None,
    runtime_override: bool = False,
    interpolation_environment: dict[str, str] | None = None,
) -> dict:
    """Parse a temporary contract with synthetic env files; never run Docker."""

    runtime = (
        SYNTHETIC_RUNTIME_ENVIRONMENT
        if runtime_environment is None
        else runtime_environment
    )
    with tempfile.TemporaryDirectory(prefix="getnewsapi-compose-test-") as directory:
        root = Path(directory)
        staged_compose = root / compose_path.name
        staged_compose.write_text(
            compose_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        default_env = root / ".env"
        default_env.write_text(
            "".join(f"{name}={value}\n" for name, value in runtime.items()),
            encoding="utf-8",
        )
        overrides = {
            "COMPOSE_DISABLE_ENV_FILE": "1",
            "GETNEWSAPI_RUNTIME_ENV_FILE": "",
            **(interpolation_environment or {}),
        }
        if runtime_override:
            custom_env = root / "synthetic-runtime.env"
            custom_env.write_text(
                "".join(f"{name}={value}\n" for name, value in runtime.items()),
                encoding="utf-8",
            )
            # Different default values detect accidental injection from .env.
            default_env.write_text(
                "DB_HOST=unused-default.example.invalid\n", encoding="utf-8"
            )
            overrides["GETNEWSAPI_RUNTIME_ENV_FILE"] = str(custom_env)

        parsed = subprocess.run(
            [
                executable,
                "compose",
                "--project-name",
                "getnewsapi-contract-test",
                "--env-file",
                str(default_env),
                "-f",
                str(staged_compose),
                "config",
                "--format",
                "json",
                "--no-path-resolution",
            ],
            cwd=root,
            env=compose_subprocess_environment(overrides),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if parsed.returncode != 0:
            raise AssertionError(
                f"{compose_path.name}: synthetic Compose validation failed: "
                f"{parsed.stderr.strip()}"
            )
        return json.loads(parsed.stdout)


class DockerPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerignore = DOCKERIGNORE_PATH.read_text(encoding="utf-8")
        cls.dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    def test_dockerignore_excludes_real_environment_files(self):
        patterns = set(self.dockerignore.splitlines())
        self.assertTrue({".env", ".env.*", "**/.env", "**/.env.*"} <= patterns)
        self.assertIn("GetNewsAPI/.env", patterns)

    def test_dockerignore_excludes_protected_artifacts(self):
        patterns = set(self.dockerignore.splitlines())
        self.assertIn("*.log", patterns)
        self.assertIn("GetNewsAPI/gpt_processor.log", patterns)
        self.assertIn("image-search-v2.diff", patterns)
        self.assertIn("image-search-v2-reviewed.diff", patterns)

    def test_dockerignore_keeps_environment_example_available(self):
        lines = self.dockerignore.splitlines()
        self.assertGreater(lines.index("!.env.example"), lines.index(".env.*"))
        self.assertGreater(lines.index("!**/.env.example"), lines.index("**/.env.*"))
        self.assertTrue(ENV_EXAMPLE_PATH.is_file())

    def test_dockerignore_does_not_exclude_migrations(self):
        self.assertNotIn("*.sql", set(self.dockerignore.splitlines()))
        self.assertNotIn("maintenance/", set(self.dockerignore.splitlines()))

    def test_all_safe_feature_defaults_remain_false(self):
        config_source = CONFIG_PATH.read_text(encoding="utf-8")
        environment_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        for name in (
            "ENABLE_APSCHEDULER",
            "PROCESS_DURABLE_CLAIMS_ENABLED",
            "PUBLISH_DURABLE_STATE_ENABLED",
            "DUPLICATE_SHADOW_ENABLED",
            "VECTOR_ENABLED",
            "EMBEDDING_ENABLED",
            "SEMANTIC_SHADOW_ENABLED",
        ):
            with self.subTest(variable=name):
                self.assertIn(f'_env_bool("{name}", False)', config_source)
                self.assertIn(f"{name}=false", environment_example)

    def test_image_search_v1_remains_the_source_default(self):
        source = CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn('os.getenv("IMAGE_SEARCH_ENGINE", "v1")', source)

    def test_dockerfile_runs_as_non_root_with_immutable_source(self):
        self.assertIn("USER 10001:10001", self.dockerfile)
        self.assertIn("WORKDIR /app", self.dockerfile)
        self.assertIn("COPY GetNewsAPI/ .", self.dockerfile)
        self.assertIn("chown 10001:10001 /data", self.dockerfile)
        self.assertNotIn("--chown=10001", self.dockerfile)
        self.assertNotIn("--reload", self.dockerfile)

    def test_dockerfile_packages_operational_migrations_without_test_fixtures(self):
        for resource_dir in (
            "maintenance/migrations",
            "maintenance/sql",
            "maintenance/vector_migrations",
        ):
            self.assertIn(
                f"COPY {resource_dir}/ /app/{resource_dir}/",
                self.dockerfile,
            )
        self.assertNotIn("maintenance/testing", self.dockerfile)

    def test_dockerfile_healthcheck_needs_no_extra_operating_system_tools(self):
        self.assertIn("urllib.request.urlopen", self.dockerfile)
        self.assertIn("http://127.0.0.1:5000/health", self.dockerfile)
        self.assertNotIn("apt-get", self.dockerfile)

    def test_startup_does_not_run_migrations_or_backfills(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertNotIn("migration", source.lower())
        self.assertNotIn("backfill", source.lower())


class RemoteComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.executable = compose_cli()
        cls.contracts = [
            (path, service_name, volume_name, resolved_compose(cls.executable, path))
            for path, service_name, volume_name in REMOTE_COMPOSE_CONTRACTS
        ]

    def test_remote_contracts_define_only_their_distinct_api_service(self):
        for path, service_name, _volume_name, model in self.contracts:
            with self.subTest(compose=path.name):
                self.assertEqual(set(model["services"]), {service_name})
                self.assertNotIn("container_name", model["services"][service_name])
                self.assertNotIn("depends_on", model["services"][service_name])

    def test_api_builds_the_root_dockerfile_and_starts_app(self):
        for path, service_name, _volume_name, model in self.contracts:
            with self.subTest(compose=path.name):
                service = model["services"][service_name]
                self.assertEqual(service["build"]["context"], ".")
                self.assertEqual(service["build"]["dockerfile"], "Dockerfile")
                self.assertEqual(service["command"], ["python", "app.py"])
                self.assertIsNone(service.get("entrypoint"))
                self.assertNotIn("user", service)

    def test_runtime_env_file_injects_values_and_invariants_override_conflicts(self):
        expected = {
            **SYNTHETIC_RUNTIME_ENVIRONMENT,
            **DEPLOYMENT_INVARIANTS,
        }
        for path, service_name, _volume_name, model in self.contracts:
            with self.subTest(compose=path.name):
                self.assertEqual(model["services"][service_name]["environment"], expected)

    def test_operator_can_select_a_separate_runtime_env_file(self):
        runtime = {
            **SYNTHETIC_RUNTIME_ENVIRONMENT,
            "DB_HOST": "explicit-runtime.example.invalid",
            "VECTOR_DB_HOST": "explicit-vector.example.invalid",
        }
        for path, service_name, _volume_name in REMOTE_COMPOSE_CONTRACTS:
            with self.subTest(compose=path.name):
                model = resolved_compose(
                    self.executable, path,
                    runtime_environment=runtime,
                    runtime_override=True,
                )
                self.assertEqual(
                    model["services"][service_name]["environment"],
                    {**runtime, **DEPLOYMENT_INVARIANTS},
                )

    def test_api_does_not_receive_provisioning_values_from_interpolation(self):
        provisioning = {
            "VECTOR_MARIADB_DATABASE": "synthetic_provisioning_database",
            "VECTOR_MARIADB_USER": "synthetic_provisioning_user",
            "VECTOR_MARIADB_PASSWORD": "synthetic-provisioning-password",
            "VECTOR_MARIADB_ROOT_PASSWORD": "synthetic-root-password",
        }
        for path, service_name, _volume_name in REMOTE_COMPOSE_CONTRACTS:
            with self.subTest(compose=path.name):
                model = resolved_compose(
                    self.executable, path, interpolation_environment=provisioning
                )
                environment = model["services"][service_name]["environment"]
                self.assertFalse(set(provisioning) & set(environment))
                self.assertNotIn("MARIADB_ROOT_PASSWORD", environment)

    def test_api_only_exposes_internal_http_port(self):
        for path, service_name, _volume_name, model in self.contracts:
            with self.subTest(compose=path.name):
                service = model["services"][service_name]
                self.assertEqual([str(port) for port in service["expose"]], ["5000"])
                self.assertNotIn("ports", service)
                self.assertNotIn("network_mode", service)

    def test_api_joins_the_existing_external_dokploy_network(self):
        for path, service_name, _volume_name, model in self.contracts:
            with self.subTest(compose=path.name):
                service = model["services"][service_name]
                self.assertEqual(set(service["networks"]), {"dokploy-network"})
                self.assertEqual(set(model["networks"]), {"dokploy-network"})
                network = model["networks"]["dokploy-network"]
                self.assertEqual(network["name"], "dokploy-network")
                self.assertIs(network["external"], True)
                self.assertNotIn("internal", network)

    def test_data_uses_isolated_project_owned_persistent_volumes(self):
        physical_names = set()
        for path, service_name, volume_name, model in self.contracts:
            with self.subTest(compose=path.name):
                self.assertEqual(set(model["volumes"]), {volume_name})
                volume = model["volumes"][volume_name]
                self.assertNotIn("external", volume)
                self.assertNotIn("driver_opts", volume)
                self.assertEqual(
                    volume["name"], f"getnewsapi-contract-test_{volume_name}"
                )
                physical_names.add(volume["name"])
                mounts = model["services"][service_name]["volumes"]
                self.assertEqual(len(mounts), 1)
                self.assertEqual(mounts[0]["type"], "volume")
                self.assertEqual(mounts[0]["source"], volume_name)
                self.assertEqual(mounts[0]["target"], "/data")
                self.assertFalse(mounts[0].get("read_only", False))
        self.assertEqual(len(physical_names), 2)

    def test_api_has_bounded_writable_paths_under_read_only_root(self):
        for path, service_name, _volume_name, model in self.contracts:
            with self.subTest(compose=path.name):
                service = model["services"][service_name]
                self.assertIs(service["read_only"], True)
                self.assertEqual(service["tmpfs"], ["/tmp:size=64m,mode=1777"])
                self.assertEqual(
                    {mount["target"] for mount in service["volumes"]}, {"/data"}
                )

    def test_api_retains_process_and_privilege_hardening(self):
        for path, service_name, _volume_name, model in self.contracts:
            with self.subTest(compose=path.name):
                service = model["services"][service_name]
                self.assertIs(service["init"], True)
                self.assertEqual(service["cap_drop"], ["ALL"])
                self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
                self.assertEqual(service["restart"], "unless-stopped")
                self.assertFalse(service.get("privileged", False))
                self.assertNotIn("cap_add", service)
                self.assertNotIn("devices", service)
                self.assertNotIn("pid", service)

    def test_liveness_uses_health_instead_of_external_database_readiness(self):
        for path, service_name, _volume_name, model in self.contracts:
            with self.subTest(compose=path.name):
                check = model["services"][service_name]["healthcheck"]
                self.assertEqual(check["test"][:3], ["CMD", "python", "-c"])
                self.assertIn("http://127.0.0.1:5000/health", check["test"][3])
                self.assertIn("timeout=3", check["test"][3])
                self.assertNotIn("/ready", " ".join(check["test"]))
                self.assertEqual(check["interval"], "30s")
                self.assertEqual(check["timeout"], "5s")
                self.assertEqual(check["start_period"], "10s")
                self.assertEqual(check["retries"], 3)

    def test_remote_yaml_defines_no_credentials_or_legacy_container_identity(self):
        for path, _service_name, _volume_name in REMOTE_COMPOSE_CONTRACTS:
            with self.subTest(compose=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("prod-getnewsapi", source)
                self.assertNotIn("dev-getnewsapi", source)
                self.assertNotIn("./GetNewsAPI:/app", source)
                self.assertNotIn("VECTOR_MARIADB_", source)
                self.assertNotIn("MARIADB_ROOT_PASSWORD", source)
                for name, value in SYNTHETIC_RUNTIME_ENVIRONMENT.items():
                    if any(marker in name for marker in ("PASSWORD", "TOKEN", "KEY")):
                        self.assertNotIn(name, source)
                        self.assertNotIn(value, source)


class DeploymentTopologyRegressionTests(unittest.TestCase):
    def test_editor_workspace_is_standalone_and_keeps_source_mount_local(self):
        local_dir = REPOSITORY_DIR / ".devcontainer"
        model = resolved_compose(
            compose_cli(), local_dir / "docker-compose.yml", runtime_environment={}
        )
        self.assertEqual(set(model["services"]), {"getnewsapi-local"})
        service = model["services"]["getnewsapi-local"]
        self.assertEqual(service["build"]["context"], "..")
        self.assertEqual(service["build"]["dockerfile"], "Dockerfile")
        self.assertEqual(service["command"], ["sleep", "infinity"])
        self.assertIs(service["healthcheck"]["disable"], True)
        self.assertEqual(len(service["volumes"]), 1)
        self.assertEqual(service["volumes"][0]["type"], "bind")
        self.assertEqual(service["volumes"][0]["target"], "/workspaces/api-test")
        self.assertNotIn("dokploy-network", model.get("networks", {}))
        for name in ("devcontainer.json", "devcontainer.jsonc"):
            source = (local_dir / name).read_text(encoding="utf-8")
            # Both committed editor configurations contain whole-line comments.
            configuration = json.loads(
                "\n".join(
                    line for line in source.splitlines()
                    if not line.lstrip().startswith("//")
                )
            )
            with self.subTest(configuration=name):
                self.assertEqual(configuration["dockerComposeFile"], ["docker-compose.yml"])
                self.assertEqual(configuration["service"], "getnewsapi-local")
                self.assertNotIn("postCreateCommand", configuration)

    def test_only_one_production_contract_remains_and_no_reference_survives(self):
        retired_name = "docker-compose." + "prod.yml"
        self.assertFalse((REPOSITORY_DIR / retired_name).exists())
        roots = [
            REPOSITORY_DIR / "docs",
            REPOSITORY_DIR / "maintenance",
            REPOSITORY_DIR / ".devcontainer",
        ]
        text_suffixes = {
            ".md", ".txt", ".py", ".sh", ".ps1", ".bat", ".cmd",
            ".json", ".jsonc", ".yaml", ".yml",
        }
        files = [
            path
            for root in roots
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in text_suffixes
        ]
        files.extend(
            path for path in REPOSITORY_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in text_suffixes
        )
        for path in files:
            with self.subTest(path=path.relative_to(REPOSITORY_DIR).as_posix()):
                self.assertNotIn(retired_name, path.read_text(encoding="utf-8"))

    def test_runtime_and_local_provisioning_examples_remain_separate(self):
        runtime = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        provisioning = PROVISIONING_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        for name in (
            "VECTOR_MARIADB_DATABASE",
            "VECTOR_MARIADB_USER",
            "VECTOR_MARIADB_PASSWORD",
            "VECTOR_MARIADB_ROOT_PASSWORD",
        ):
            self.assertNotIn(name, runtime)
            self.assertIn(name, provisioning)
        for name in (
            "VECTOR_DB_HOST",
            "VECTOR_DB_PORT",
            "VECTOR_DB_NAME",
            "VECTOR_DB_USER",
            "VECTOR_DB_PASSWORD",
        ):
            self.assertIn(f"{name}=", runtime)
        self.assertIn("LOCAL", provisioning.upper())

    def test_local_vector_helper_is_loopback_bound_and_owns_only_local_database(self):
        executable = compose_cli()
        path = REPOSITORY_DIR / "maintenance" / "vector" / "docker-compose.vector.yml"
        model = resolved_compose(executable, path, runtime_environment={})
        self.assertEqual(set(model["services"]), {"getnewsapi-vector-mariadb"})
        service = model["services"]["getnewsapi-vector-mariadb"]
        self.assertEqual(service["image"], "mariadb:11.8")
        self.assertEqual(len(service["ports"]), 1)
        self.assertEqual(service["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(str(service["ports"][0]["published"]), "13309")
        self.assertEqual(service["ports"][0]["target"], 3306)
        self.assertIn("MARIADB_ROOT_PASSWORD", service["environment"])
        self.assertEqual(service["volumes"][0]["type"], "volume")
        self.assertEqual(service["volumes"][0]["target"], "/var/lib/mysql")
        readme = path.with_name("README.md").read_text(encoding="utf-8")
        self.assertIn("LOCAL", readme.upper())
        self.assertIn("NOT FOR", readme.upper())
        self.assertIn("production", readme.lower())


class ConfigurationValidationTests(unittest.TestCase):
    def test_wordpress_http_timeout_defaults_are_explicit(self):
        import config

        self.assertEqual(config.WP_HTTP_CONNECT_TIMEOUT_SECONDS, 10)
        self.assertEqual(config.WP_HTTP_READ_TIMEOUT_SECONDS, 60)
        environment_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        self.assertIn("WP_HTTP_CONNECT_TIMEOUT_SECONDS=10", environment_example)
        self.assertIn("WP_HTTP_READ_TIMEOUT_SECONDS=60", environment_example)

    def test_wordpress_http_timeouts_are_positive_and_bounded(self):
        cases = (
            ("WP_HTTP_CONNECT_TIMEOUT_SECONDS", "0"),
            ("WP_HTTP_CONNECT_TIMEOUT_SECONDS", "61"),
            ("WP_HTTP_READ_TIMEOUT_SECONDS", "0"),
            ("WP_HTTP_READ_TIMEOUT_SECONDS", "301"),
            ("WP_HTTP_READ_TIMEOUT_SECONDS", "not-a-number"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value):
                environment = minimal_web_environment()
                environment[name] = value
                rendered = format_config_issues(validate_runtime_config(environment))
                self.assertIn(f"{name}: must be a positive integer", rendered)

    def test_config_check_accepts_valid_minimal_web_configuration(self):
        environment = minimal_web_environment()
        self.assertEqual(validate_runtime_config(environment), ())

        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
            self.assertTrue(tasks.run_config_check("web"))
        self.assertIn("Configuration valid for profile: web", output.getvalue())

    def test_config_check_reports_missing_core_configuration(self):
        environment = minimal_web_environment()
        environment.pop("DB_HOST")
        issues = validate_runtime_config(environment)
        self.assertIn("DB_HOST: is required", format_config_issues(issues))

    def test_disabled_optional_features_do_not_require_provider_keys(self):
        environment = minimal_web_environment()
        for name in (
            "CRYPTO_NEWS_TOKEN",
            "GROK_API_KEY",
            "OPENAI_API_KEY",
            "WP_API_URL",
            "VECTOR_DB_PASSWORD",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(validate_runtime_config(environment), ())

    def test_enabled_vector_feature_requires_complete_vector_configuration(self):
        environment = minimal_web_environment()
        environment["VECTOR_ENABLED"] = "true"
        rendered = format_config_issues(validate_runtime_config(environment))
        for name in (
            "VECTOR_DB_USER",
            "VECTOR_DB_PASSWORD",
            "VECTOR_DB_HOST",
            "VECTOR_DB_NAME",
        ):
            self.assertIn(f"{name}: is required", rendered)

    def test_enabled_openai_embedding_requires_api_key(self):
        environment = minimal_web_environment()
        environment.update(
            {
                "VECTOR_ENABLED": "true",
                "VECTOR_DB_USER": "vector_user",
                "VECTOR_DB_PASSWORD": "vector-secret-value",
                "VECTOR_DB_HOST": "vector.internal",
                "VECTOR_DB_NAME": "coincourier_vectors",
                "EMBEDDING_ENABLED": "true",
                "EMBEDDING_PROVIDER": "openai",
                "EMBEDDING_MODEL": "text-embedding-3-small",
                "EMBEDDING_CHUNKER_VERSION": "chunk-v1",
                "EMBEDDING_DIMENSIONS": "1536",
            }
        )
        rendered = format_config_issues(validate_runtime_config(environment))
        self.assertIn("OPENAI_API_KEY: is required for OpenAI embeddings", rendered)

    def test_semantic_shadow_requires_vector_feature(self):
        environment = minimal_web_environment()
        environment["SEMANTIC_SHADOW_ENABLED"] = "true"
        rendered = format_config_issues(validate_runtime_config(environment))
        self.assertIn(
            "SEMANTIC_SHADOW_ENABLED: requires VECTOR_ENABLED=true",
            rendered,
        )

    def test_validator_output_never_contains_secret_values(self):
        environment = minimal_web_environment()
        environment.pop("DB_HOST")
        rendered = format_config_issues(validate_runtime_config(environment))
        self.assertNotIn(environment["DB_PASSWORD"], rendered)
        self.assertNotIn(environment["PUBLISH_API_TOKEN"], rendered)

    def test_config_check_makes_no_database_or_network_connection(self):
        environment = minimal_web_environment()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("mysql.connector.connect") as connect,
            patch("socket.create_connection") as create_connection,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertTrue(tasks.run_config_check("web"))
        connect.assert_not_called()
        create_connection.assert_not_called()

    def test_config_check_command_returns_nonzero_for_invalid_configuration(self):
        environment = minimal_web_environment()
        environment.pop("DB_NAME")
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "argv", ["tasks.py", "config_check"]),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(tasks.main(), 1)

    def test_config_check_handles_malformed_numeric_values_safely(self):
        environment = minimal_web_environment()
        environment["DB_PORT"] = "not-a-port-secret"
        errors = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=True),
            redirect_stderr(errors),
        ):
            self.assertFalse(tasks.run_config_check("web"))

        self.assertIn("DB_PORT: must be a positive integer", errors.getvalue())
        self.assertNotIn("not-a-port-secret", errors.getvalue())

    def test_tls_verification_requires_supported_ca_contract(self):
        environment = minimal_web_environment()
        environment["DB_SSL_VERIFY_CERT"] = "true"
        rendered = format_config_issues(validate_runtime_config(environment))
        self.assertIn("DB_SSL_CA: is required", rendered)


class RuntimeContractTests(unittest.TestCase):
    def test_runbook_uses_repository_root_for_checkout_commands(self):
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        self.assertIn("Run every checkout command in this runbook from the repository root", runbook)
        self.assertNotRegex(runbook, r"(?m)^python tasks\.py")
        self.assertIn("python GetNewsAPI/tasks.py job_catalog", runbook)
        self.assertIn("python -m unittest GetNewsAPI.tests.test_semantic_retrieval", runbook)
        self.assertIn(
            "docker compose -f maintenance/vector/docker-compose.vector.yml",
            runbook,
        )

    def test_documented_remote_compose_validation_uses_runtime_environment(self):
        documents = (
            RUNBOOK_PATH.read_text(encoding="utf-8"),
            PRODUCTION_ENVIRONMENT_PATH.read_text(encoding="utf-8"),
        )
        for document in documents:
            self.assertNotIn("GETNEWSAPI_ENV_FILE", document)
            self.assertIn("GETNEWSAPI_RUNTIME_ENV_FILE", document)
            self.assertIn("config --quiet", document)
            self.assertIn("docker-compose.yml", document)
            self.assertIn("docker-compose.dev.yml", document)
            self.assertIn("getnewsapi-prod", document)
            self.assertIn("getnewsapi-dev", document)

    def test_backfill_documentation_does_not_claim_a_total_scan_bound(self):
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        production_environment = PRODUCTION_ENVIRONMENT_PATH.read_text(
            encoding="utf-8"
        )
        self.assertIn("limit bounds changed document/job", runbook)
        self.assertIn("not a total\nscan/page ceiling", runbook)
        self.assertIn("do not impose a total scan ceiling", production_environment)

    def test_stdout_logging_is_default_and_file_logging_is_opt_in(self):
        environment_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        self.assertIn("LOG_LEVEL=INFO", environment_example)
        self.assertIn("FILE_LOGGING_ENABLED=false", environment_example)

        with (
            patch.object(logging_config, "FILE_LOGGING_ENABLED", False),
            patch.object(logging, "basicConfig") as basic_config,
        ):
            logging_config.configure_logging()
        handlers = basic_config.call_args.kwargs["handlers"]
        self.assertEqual(len(handlers), 1)
        self.assertNotIsInstance(handlers[0], logging.FileHandler)

    def test_writable_state_paths_are_rooted_in_data_volume(self):
        config_source = CONFIG_PATH.read_text(encoding="utf-8")
        compose = PRODUCTION_COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertIn('"/data" if APP_ENV == "production" else "/app/cache"', config_source)
        self.assertIn("STOCK_IMAGE_CACHE_DIR: /data/cache/stock_images", compose)
        self.assertIn("STOCK_IMAGE_USAGE_PATH: /data/stock_image_usage.json", compose)

    def test_database_tls_options_match_installed_connector_api(self):
        from mysql.connector.abstracts import DEFAULT_CONFIGURATION

        supported = set(DEFAULT_CONFIGURATION)
        self.assertTrue(
            {"ssl_disabled", "ssl_verify_cert", "ssl_verify_identity", "ssl_ca"}
            <= supported
        )
        source = CONFIG_PATH.read_text(encoding="utf-8")
        for prefix in ("DB", "VECTOR_DB", "WP_DB"):
            self.assertIn(f'_db_tls_options("{prefix}")', source)

    def test_runtime_dependencies_are_deliberately_pinned(self):
        requirements = [
            line.strip()
            for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertTrue(requirements)
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=\s]+", line) for line in requirements))
        names = {line.split("==", 1)[0].lower() for line in requirements}
        self.assertTrue(
            {
                "fastapi",
                "uvicorn",
                "mysql-connector-python",
                "apscheduler",
                "requests",
                "httpx",
                "pillow",
                "openai",
                "python-dotenv",
            }
            <= names
        )


if __name__ == "__main__":
    unittest.main()
