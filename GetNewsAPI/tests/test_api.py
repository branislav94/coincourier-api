from __future__ import annotations

import asyncio
import importlib.util
import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


PROJECT_DIR = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_DIR / "app.py"
SCHEDULER_PATH = PROJECT_DIR / "scheduler.py"
REFACTOR_PLAN_PATH = PROJECT_DIR.parent / "docs" / "GETNEWSAPI_INCREMENTAL_REFACTOR_PLAN.md"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import app as api_app
import tasks


def connection_with_rows(rows):
    cursor = Mock()
    cursor.fetchall.return_value = rows
    connection = Mock()
    connection.cursor.return_value = cursor
    return connection, cursor


def load_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


class ApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.scheduler_flag = patch.object(api_app, "ENABLE_APSCHEDULER", False)
        self.scheduler_flag.start()
        self.addCleanup(self.scheduler_flag.stop)
        api_app.app.state.scheduler_references = None
        self.addCleanup(setattr, api_app.app.state, "scheduler_references", None)

    @staticmethod
    def client():
        return TestClient(api_app.app, raise_server_exceptions=False)

    def test_health_returns_service_status_without_side_effects(self):
        with (
            patch.object(api_app, "get_db_connection") as connect,
            patch.object(api_app, "publish_news_to_wp") as publish,
            patch.object(api_app, "start_scheduler") as start,
            self.client() as client,
        ):
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "GetNewsAPI"})
        connect.assert_not_called()
        publish.assert_not_called()
        start.assert_not_called()

    def test_news_returns_previous_success_shape(self):
        rows = [
            {"id": 7, "title": "Newest", "publish_date": "2026-07-26 12:00:00"},
            {"id": 6, "title": "Older", "publish_date": "2026-07-26 11:00:00"},
        ]
        connection, cursor = connection_with_rows(rows)

        with patch.object(api_app, "get_db_connection", return_value=connection):
            with self.client() as client:
                response = client.get("/api/news")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), rows)
        connection.cursor.assert_called_once_with(dictionary=True)
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_news_preserves_limit_and_descending_order_query(self):
        rows = [
            {"id": row_id, "publish_date": f"2026-07-{row_id:02d} 12:00:00"}
            for row_id in range(7, 0, -1)
        ]
        connection, cursor = connection_with_rows(rows)

        with patch.object(api_app, "get_db_connection", return_value=connection):
            with self.client() as client:
                response = client.get("/api/news")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), rows)
        self.assertEqual(len(response.json()), 7)
        cursor.execute.assert_called_once_with(
            "SELECT * FROM rich_crpytonews ORDER BY publish_date DESC LIMIT 7;"
        )

    def test_news_database_failure_preserves_500_response_structure(self):
        with (
            patch.object(
                api_app,
                "get_db_connection",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch.object(api_app.logger, "error"),
            self.client() as client,
        ):
            response = client.get("/api/news")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(response.text, api_app.INTERNAL_SERVER_ERROR_BODY)

    def test_publish_calls_wordpress_publisher_once(self):
        with patch.object(api_app, "publish_news_to_wp") as publish:
            with self.client() as client:
                response = client.post("/api/publish")

        self.assertEqual(response.status_code, 200)
        publish.assert_called_once_with()

    def test_publish_success_preserves_response_shape(self):
        with patch.object(api_app, "publish_news_to_wp"):
            with self.client() as client:
                response = client.post("/api/publish")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "success", "message": "News published to WordPress."},
        )

    def test_publish_failure_preserves_500_response_structure(self):
        with (
            patch.object(
                api_app,
                "publish_news_to_wp",
                side_effect=RuntimeError("WordPress unavailable"),
            ),
            patch.object(api_app.logger, "error"),
            self.client() as client,
        ):
            response = client.post("/api/publish")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(response.text, api_app.INTERNAL_SERVER_ERROR_BODY)

    def test_scheduler_disabled_starts_nothing(self):
        with (
            patch.object(api_app, "start_scheduler") as start,
            patch.object(api_app, "stop_scheduler") as stop,
            self.client() as client,
        ):
            self.assertEqual(client.get("/health").status_code, 200)

        start.assert_not_called()
        stop.assert_not_called()

    def test_scheduler_enabled_repeated_lifespans_restart_cleanly(self):
        first_references = (
            Mock(name="first_fetch_scheduler"),
            Mock(name="first_chained_scheduler"),
        )
        second_references = (
            Mock(name="second_fetch_scheduler"),
            Mock(name="second_chained_scheduler"),
        )

        async def exercise_sequential_lifespans():
            async with api_app.lifespan(api_app.app):
                self.assertIs(api_app.app.state.scheduler_references, first_references)
            self.assertIsNone(api_app.app.state.scheduler_references)

            async with api_app.lifespan(api_app.app):
                self.assertIs(api_app.app.state.scheduler_references, second_references)
            self.assertIsNone(api_app.app.state.scheduler_references)

        with (
            patch.object(api_app, "ENABLE_APSCHEDULER", True),
            patch.object(
                api_app,
                "start_scheduler",
                side_effect=[first_references, second_references],
            ) as start,
            patch.object(api_app, "stop_scheduler") as stop,
        ):
            asyncio.run(exercise_sequential_lifespans())

        self.assertEqual(start.call_count, 2)
        self.assertEqual(stop.call_count, 2)

    def test_application_shutdown_stops_schedulers_cleanly(self):
        references = (Mock(name="fetch_scheduler"), Mock(name="chained_scheduler"))
        with (
            patch.object(api_app, "ENABLE_APSCHEDULER", True),
            patch.object(api_app, "start_scheduler", return_value=references),
            patch.object(api_app, "stop_scheduler") as stop,
        ):
            with self.client() as client:
                self.assertEqual(client.get("/health").status_code, 200)
                stop.assert_not_called()
            stop.assert_called_once_with()

        self.assertIsNone(api_app.app.state.scheduler_references)

    def test_all_routes_are_exercised_with_mocked_boundaries(self):
        connection, _cursor = connection_with_rows([])
        with (
            patch.object(api_app, "get_db_connection", return_value=connection) as connect,
            patch.object(api_app, "publish_news_to_wp") as publish,
            patch.object(api_app, "start_scheduler") as start,
            self.client() as client,
        ):
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/api/news").status_code, 200)
            self.assertEqual(client.post("/api/publish").status_code, 200)

        connect.assert_called_once_with()
        publish.assert_called_once_with()
        start.assert_not_called()


class ApiImportAndCommandTests(unittest.TestCase):
    def test_documented_reload_commands_do_not_combine_workers(self):
        document = REFACTOR_PLAN_PATH.read_text(encoding="utf-8")
        expected = (
            "python -m uvicorn app:app --reload --host 127.0.0.1 --port 5000"
        )

        self.assertIn(expected, document)
        for line in document.splitlines():
            if "--reload" in line:
                self.assertNotIn("--workers", line)

    def test_repeated_application_imports_do_not_start_schedulers(self):
        fake_scheduler = types.ModuleType("scheduler")
        fake_scheduler.start_scheduler = Mock()
        fake_scheduler.stop_scheduler = Mock()

        with patch.dict(sys.modules, {"scheduler": fake_scheduler}):
            load_module_from_path("_app_import_probe_one", APP_PATH)
            load_module_from_path("_app_import_probe_two", APP_PATH)

        fake_scheduler.start_scheduler.assert_not_called()
        fake_scheduler.stop_scheduler.assert_not_called()

    def test_importing_app_does_not_start_uvicorn(self):
        fake_uvicorn = types.ModuleType("uvicorn")
        fake_uvicorn.run = Mock()

        with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
            load_module_from_path("_app_uvicorn_import_probe", APP_PATH)

        fake_uvicorn.run.assert_not_called()

    def test_python_app_uses_expected_uvicorn_settings(self):
        fake_uvicorn = types.ModuleType("uvicorn")
        fake_uvicorn.run = Mock()

        with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
            runpy.run_path(str(APP_PATH), run_name="__main__")

        fake_uvicorn.run.assert_called_once()
        application = fake_uvicorn.run.call_args.args[0]
        self.assertIsInstance(application, FastAPI)
        self.assertEqual(
            fake_uvicorn.run.call_args.kwargs,
            {
                "host": "0.0.0.0",
                "port": 5000,
                "workers": 1,
                "reload": False,
            },
        )

    def test_scheduler_module_starts_each_scheduler_once_and_stops_both(self):
        fetch_reference = Mock(name="fetch_scheduler")
        chained_reference = Mock(name="chained_scheduler")
        chained_reference.running = True

        background_module = types.ModuleType("apscheduler.schedulers.background")
        background_module.BackgroundScheduler = Mock(return_value=chained_reference)
        schedulers_module = types.ModuleType("apscheduler.schedulers")
        apscheduler_module = types.ModuleType("apscheduler")

        fetcher_module = types.ModuleType("fetcher")
        fetcher_module.start_scheduler = Mock(return_value=fetch_reference)
        fetcher_module.stop_scheduler = Mock()
        processor_module = types.ModuleType("gpt_processor")
        processor_module.process_news_with_gpt = Mock()
        publisher_module = types.ModuleType("publish_to_wp")
        publisher_module.publish_news_to_wp = Mock()

        fake_modules = {
            "apscheduler": apscheduler_module,
            "apscheduler.schedulers": schedulers_module,
            "apscheduler.schedulers.background": background_module,
            "fetcher": fetcher_module,
            "gpt_processor": processor_module,
            "publish_to_wp": publisher_module,
        }
        with patch.dict(sys.modules, fake_modules):
            scheduler = load_module_from_path("_scheduler_lifecycle_probe", SCHEDULER_PATH)
            first = scheduler.start_scheduler()
            second = scheduler.start_scheduler()
            scheduler.stop_scheduler()

        self.assertEqual(first, (fetch_reference, chained_reference))
        self.assertEqual(second, first)
        fetcher_module.start_scheduler.assert_called_once_with()
        background_module.BackgroundScheduler.assert_called_once_with()
        chained_reference.add_job.assert_called_once()
        chained_reference.start.assert_called_once_with()
        chained_reference.shutdown.assert_called_once_with(wait=False)
        fetcher_module.stop_scheduler.assert_called_once_with(wait=False)

    def test_scheduler_partial_startup_stops_started_schedulers(self):
        fetch_reference = Mock(name="fetch_scheduler")
        chained_reference = Mock(name="chained_scheduler")
        chained_reference.running = False

        def fail_after_chained_start():
            chained_reference.running = True
            raise RuntimeError("chained scheduler startup failed")

        chained_reference.start.side_effect = fail_after_chained_start
        background_module = types.ModuleType("apscheduler.schedulers.background")
        background_module.BackgroundScheduler = Mock(return_value=chained_reference)
        schedulers_module = types.ModuleType("apscheduler.schedulers")
        apscheduler_module = types.ModuleType("apscheduler")

        fetcher_module = types.ModuleType("fetcher")
        fetcher_module.start_scheduler = Mock(return_value=fetch_reference)
        fetcher_module.stop_scheduler = Mock()
        processor_module = types.ModuleType("gpt_processor")
        processor_module.process_news_with_gpt = Mock()
        publisher_module = types.ModuleType("publish_to_wp")
        publisher_module.publish_news_to_wp = Mock()

        fake_modules = {
            "apscheduler": apscheduler_module,
            "apscheduler.schedulers": schedulers_module,
            "apscheduler.schedulers.background": background_module,
            "fetcher": fetcher_module,
            "gpt_processor": processor_module,
            "publish_to_wp": publisher_module,
        }
        with patch.dict(sys.modules, fake_modules):
            scheduler = load_module_from_path("_scheduler_failure_probe", SCHEDULER_PATH)
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                scheduler.start_scheduler()

        chained_reference.shutdown.assert_called_once_with(wait=False)
        fetcher_module.stop_scheduler.assert_called_once_with(wait=False)
        self.assertIsNone(scheduler._fetch_scheduler)
        self.assertIsNone(scheduler._scheduler)

    def test_tasks_dispatch_remains_importable_and_unchanged(self):
        command_to_runner = {
            "fetch": "run_fetch",
            "process": "run_process",
            "publish": "run_publish",
            "chained": "run_chained",
        }

        for command, runner_name in command_to_runner.items():
            with self.subTest(command=command):
                with (
                    patch.object(tasks, runner_name) as runner,
                    patch.object(sys, "argv", ["tasks.py", command]),
                ):
                    self.assertEqual(tasks.main(), 0)
                runner.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
