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
import unittest
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import tasks
from runtime.config_validation import format_config_issues, validate_runtime_config
from runtime import logging_config


DOCKERIGNORE_PATH = REPOSITORY_DIR / ".dockerignore"
DOCKERFILE_PATH = REPOSITORY_DIR / "Dockerfile"
PRODUCTION_COMPOSE_PATH = REPOSITORY_DIR / "docker-compose.prod.yml"
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


def production_services(compose: str) -> set[str]:
    service_block = compose.split("services:", 1)[1].split("\nnetworks:", 1)[0]
    return set(re.findall(r"^  ([a-z0-9-]+):\s*$", service_block, re.MULTILINE))


class DockerPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerignore = DOCKERIGNORE_PATH.read_text(encoding="utf-8")
        cls.dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        cls.compose = PRODUCTION_COMPOSE_PATH.read_text(encoding="utf-8")

    def test_dockerignore_excludes_real_environment_files(self):
        patterns = set(self.dockerignore.splitlines())
        self.assertTrue((PROJECT_DIR / ".env").exists())
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

    def test_production_compose_has_no_source_bind_mount(self):
        self.assertNotIn("./GetNewsAPI:/app", self.compose)
        self.assertNotIn("type: bind", self.compose)

    def test_production_compose_defines_only_web_and_vector_database_services(self):
        self.assertEqual(
            production_services(self.compose),
            {"getnewsapi-web", "getnewsapi-vector-mariadb"},
        )

    def test_production_compose_does_not_define_application_database(self):
        services = production_services(self.compose)
        self.assertNotIn("getnewsapi-mariadb", services)
        self.assertNotIn("application-db", services)

    def test_vector_database_uses_mariadb_11_8_and_persistent_volume(self):
        self.assertIn("image: mariadb:11.8", self.compose)
        self.assertIn("getnewsapi-vector-data:/var/lib/mysql", self.compose)
        self.assertRegex(self.compose, r"(?m)^  getnewsapi-vector-data:$")

    def test_vector_database_has_no_public_port(self):
        self.assertNotRegex(self.compose, r"(?m)^    ports:$")
        self.assertNotIn("3306:3306", self.compose)

    def test_production_network_is_explicit_and_shared(self):
        self.assertRegex(self.compose, r"(?m)^  getnewsapi-private:$")
        self.assertEqual(self.compose.count("- getnewsapi-private"), 2)
        self.assertRegex(
            self.compose,
            r"(?ms)^  getnewsapi-private:\s+driver: bridge\s+internal: true$",
        )
        self.assertRegex(self.compose, r"(?m)^  getnewsapi-edge:$")

    def test_production_web_exposes_only_container_port(self):
        self.assertIn('expose:\n      - "5000"', self.compose)
        self.assertNotRegex(self.compose, r"(?m)^    ports:$")

    def test_vector_provisioning_has_no_default_passwords(self):
        for name in (
            "VECTOR_MARIADB_USER",
            "VECTOR_MARIADB_PASSWORD",
            "VECTOR_MARIADB_ROOT_PASSWORD",
        ):
            self.assertIn(f"${{{name}:?", self.compose)

    def test_vector_application_connection_uses_runtime_credentials(self):
        runtime_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        self.assertIn("VECTOR_DB_HOST: getnewsapi-vector-mariadb", self.compose)
        self.assertIn('VECTOR_DB_PORT: "3306"', self.compose)
        for name in ("VECTOR_DB_NAME", "VECTOR_DB_USER", "VECTOR_DB_PASSWORD"):
            self.assertIn(f"{name}=", runtime_example)

    def test_runtime_and_provisioning_environment_contracts_are_separate(self):
        runtime_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        provisioning_example = PROVISIONING_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        self.assertTrue(PROVISIONING_ENV_EXAMPLE_PATH.is_file())
        self.assertIn("GETNEWSAPI_RUNTIME_ENV_FILE", self.compose)
        for name in (
            "VECTOR_MARIADB_DATABASE",
            "VECTOR_MARIADB_USER",
            "VECTOR_MARIADB_PASSWORD",
            "VECTOR_MARIADB_ROOT_PASSWORD",
        ):
            self.assertNotIn(name, runtime_example)
            self.assertIn(name, provisioning_example)

        web_block = self.compose.split("  getnewsapi-web:", 1)[1].split(
            "  getnewsapi-vector-mariadb:", 1
        )[0]
        self.assertNotIn("VECTOR_MARIADB_", web_block)
        self.assertNotIn("MARIADB_ROOT_PASSWORD", web_block)

    def test_resolved_compose_scopes_vector_provisioning_to_database(self):
        if shutil.which("docker") is None:
            self.skipTest("Docker CLI is not installed")

        version = subprocess.run(
            ["docker", "compose", "version"],
            cwd=REPOSITORY_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        if version.returncode != 0:
            self.skipTest("Docker Compose is not available")

        resolved = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(PROVISIONING_ENV_EXAMPLE_PATH),
                "-f",
                str(PRODUCTION_COMPOSE_PATH),
                "config",
                "--format",
                "json",
            ],
            cwd=REPOSITORY_DIR,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            resolved.returncode,
            0,
            msg="resolved Compose configuration failed",
        )
        services = json.loads(resolved.stdout)["services"]
        self.assertEqual(
            set(services),
            {"getnewsapi-web", "getnewsapi-vector-mariadb"},
        )
        web_environment = set(services["getnewsapi-web"]["environment"])
        vector_environment = set(
            services["getnewsapi-vector-mariadb"]["environment"]
        )
        self.assertTrue(
            {"VECTOR_DB_HOST", "VECTOR_DB_PORT", "VECTOR_DB_NAME",
             "VECTOR_DB_USER", "VECTOR_DB_PASSWORD"} <= web_environment
        )
        self.assertTrue(
            {"MARIADB_DATABASE", "MARIADB_USER", "MARIADB_PASSWORD",
             "MARIADB_ROOT_PASSWORD"} <= vector_environment
        )
        self.assertFalse(
            {"VECTOR_MARIADB_ROOT_PASSWORD", "MARIADB_ROOT_PASSWORD"}
            & web_environment
        )
        self.assertNotIn("ports", services["getnewsapi-vector-mariadb"])

        for flag in (
            "PROCESS_DURABLE_CLAIMS_ENABLED",
            "PUBLISH_DURABLE_STATE_ENABLED",
            "DUPLICATE_SHADOW_ENABLED",
            "VECTOR_ENABLED",
            "EMBEDDING_ENABLED",
            "SEMANTIC_SHADOW_ENABLED",
        ):
            self.assertEqual(
                services["getnewsapi-web"]["environment"][flag],
                "false",
            )

    def test_one_shot_jobs_inherit_only_the_web_service_environment(self):
        self.assertNotIn("worker:", self.compose)
        self.assertNotIn("scheduler:", self.compose)
        self.assertIn("run --rm getnewsapi-web", RUNBOOK_PATH.read_text(encoding="utf-8"))

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
            self.assertIn(f'_env_bool("{name}", False)', config_source)
            self.assertIn(f"{name}=false", environment_example)
        self.assertIn('ENABLE_APSCHEDULER: "false"', self.compose)

    def test_image_search_v1_remains_the_source_default(self):
        source = CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn('os.getenv("IMAGE_SEARCH_ENGINE", "v1")', source)

    def test_dockerfile_runs_as_non_root_with_immutable_source(self):
        self.assertIn("USER 10001:10001", self.dockerfile)
        self.assertIn("COPY GetNewsAPI/ .", self.dockerfile)
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

    def test_web_root_filesystem_is_read_only_with_bounded_writable_paths(self):
        self.assertIn("read_only: true", self.compose)
        self.assertIn("getnewsapi-state:/data", self.compose)
        self.assertIn("/tmp:size=64m,mode=1777", self.compose)

    def test_container_healthchecks_use_python_or_image_native_tools(self):
        self.assertIn("urllib.request.urlopen", self.dockerfile)
        self.assertIn("urllib.request.urlopen", self.compose)
        self.assertIn("healthcheck.sh", self.compose)
        self.assertNotIn("apt-get", self.dockerfile)

    def test_startup_does_not_run_migrations_or_backfills(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertNotIn("migration", source.lower())
        self.assertNotIn("backfill", source.lower())


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

    def test_documented_compose_validation_is_quiet_and_uses_provisioning_file(self):
        documents = (
            RUNBOOK_PATH.read_text(encoding="utf-8"),
            PRODUCTION_ENVIRONMENT_PATH.read_text(encoding="utf-8"),
        )
        for document in documents:
            self.assertNotIn("GETNEWSAPI_ENV_FILE", document)
            self.assertNotRegex(
                document,
                r"(?m)^\s*-f docker-compose\.prod\.yml config\s*$",
            )
            self.assertIn("-f docker-compose.prod.yml config --quiet", document)
            self.assertIn("getnewsapi-provisioning.env", document)

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
