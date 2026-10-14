"""Concurrency boundaries and configuration for independent backup workers."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import as_completed
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from unittest.mock import patch

import requests

from main import BackupProject
from main import GitLabToGOGSBackup
from tests import test_gitlab_retry
from tests.test_backup import make_backup
from tests.test_gitlab_retry import response

if TYPE_CHECKING:
    from collections.abc import Iterator
    from concurrent.futures import Future

# ruff: noqa: PT009, PT027, SLF001


def make_project(project_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=project_id,
        name=f"survey{project_id}",
        path_with_namespace=f"group/survey{project_id}",
        description="Test repository",
        visibility="private",
        http_url_to_repo=f"https://gitlab.test/group/survey{project_id}.git",
    )


class ParallelBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backup = make_backup()
        self.backup.backup_dir = Path(directory.name) / "mirrors"
        self.projects = [make_project(idx) for idx in range(6)]
        self.backup.gl = MagicMock()
        self.backup.gl.groups.get.return_value.projects.list.return_value = (
            self.projects
        )
        self.backup.gl.projects.get.side_effect = AssertionError(
            "Per-project GitLab requests are forbidden"
        )

    def test_parallel_thread_configuration(self) -> None:
        for value, expected in ((None, 1), ("1", 1), ("4", 4), (" 2 ", 2)):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    test_gitlab_retry.GitLabRetryTests.startup_environment,
                    clear=True,
                ),
                patch.dict(
                    os.environ, {"GIT_PROJECT_DIR": str(self.backup.backup_dir)}
                ),
                patch("main.RetryingGitlab"),
                patch("main.requests.request") as request,
            ):
                if value is not None:
                    os.environ["BACKUP_PARALLEL_THREADS"] = value
                self.assertEqual(GitLabToGOGSBackup().parallel_threads, expected)
                request.assert_not_called()

    def test_invalid_thread_count_fails_before_network(self) -> None:
        for value in ("", " ", "0", "-2", "1.5", "two", "true"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    test_gitlab_retry.GitLabRetryTests.startup_environment,
                    clear=True,
                ),
                patch.dict(
                    os.environ,
                    {
                        "GIT_PROJECT_DIR": str(self.backup.backup_dir),
                        "BACKUP_PARALLEL_THREADS": value,
                    },
                ),
                patch("main.RetryingGitlab") as client,
                patch("main.requests.request") as request,
                self.assertRaisesRegex(ValueError, "BACKUP_PARALLEL_THREADS"),
            ):
                GitLabToGOGSBackup()
            client.assert_not_called()
            request.assert_not_called()

    def test_workers_overlap_within_limit_after_listing(self) -> None:
        for limit in (1, 2, 3, 8):
            with self.subTest(limit=limit):
                self.check_parallel_limit(limit)

    def check_parallel_limit(self, limit: int) -> None:
        main_thread = threading.get_ident()
        self.backup.parallel_threads = limit
        listed = threading.Event()
        completed = []
        active = 0
        peak = 0
        lock = threading.Lock()
        barrier = threading.Barrier(min(limit, len(self.projects)), timeout=5)

        def list_projects(**kwargs: object) -> list[SimpleNamespace]:
            self.assertEqual(threading.get_ident(), main_thread)
            self.assertEqual(kwargs["per_page"], 100)
            self.assertFalse(kwargs["simple"])
            listed.set()
            return self.projects

        def work(project: BackupProject) -> None:
            nonlocal active, peak
            self.assertNotEqual(threading.get_ident(), main_thread)
            self.assertTrue(listed.is_set())
            self.assertIsInstance(project, BackupProject)
            self.assertTrue(self.backup._repository_path(project).parent.is_dir())
            with self.assertRaises(FrozenInstanceError):
                project.name = "changed"
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait()
            finally:
                with lock:
                    active -= 1
                    completed.append(project.id)

        self.backup.gl.groups.get.return_value.projects.list.side_effect = list_projects
        with (
            patch.object(self.backup, "_backup_repository", new=work),
            self.assertLogs("main", level="INFO") as logs,
        ):
            self.backup.run()
        self.assertEqual(peak, min(limit, len(self.projects)))
        self.assertEqual(sorted(completed), list(range(6)))
        self.assertEqual(active, 0)
        self.backup.gl.projects.get.assert_not_called()
        if limit == 1:
            self.assertEqual(completed, list(range(6)))
        summary = [r for r in logs.records if "Successful:" in r.getMessage()]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0].thread, main_thread)
        self.assertIn("Successful: 6 repositories", summary[0].getMessage())

    def test_listing_is_detached_and_duplicates_are_scheduled_once(self) -> None:
        prepared, failed = self.backup._prepare_projects(
            [self.projects[0], self.projects[0], self.projects[1]]
        )
        self.projects[0].description = "Changed SDK object"
        self.assertFalse(failed)
        with (
            patch.object(self.backup, "_backup_repository") as backup,
            self.assertLogs("main", level="INFO"),
        ):
            successful, failed = self.backup._backup_projects(prepared, 3)
        self.assertEqual(len(successful), 2)
        self.assertFalse(failed)
        self.backup.gl.projects.get.assert_not_called()
        self.assertEqual(backup.call_count, 2)
        first = backup.call_args_list[0].args[0]
        self.assertEqual(first.description, "Test repository")
        self.assertIsNot(first, self.projects[0])

    def test_empty_group_and_invalid_listing_records_do_not_start_pool(self) -> None:
        for projects in ([], self.projects):
            with (
                self.subTest(empty=not projects),
                patch("main.ThreadPoolExecutor") as pool,
                self.assertLogs("main", level="INFO") as logs,
            ):
                self.backup.gl.groups.get.return_value.projects.list.return_value = (
                    projects
                )
                for project in projects:
                    project.http_url_to_repo = "invalid-url"
                if projects:
                    with self.assertRaises(SystemExit) as caught:
                        self.backup.run()
                    self.assertEqual(caught.exception.code, 1)
                else:
                    self.backup.run()
            pool.assert_not_called()
            self.assertFalse(self.backup.backup_dir.exists())
            self.assertIn(
                f"Failed: {len(projects)} repositories", "\n".join(logs.output)
            )

    def test_failures_are_collected_on_main_thread_in_discovery_order(self) -> None:
        self.backup.parallel_threads = 3
        main_thread = threading.get_ident()
        release_first = threading.Event()
        completions = []

        self.projects[1].http_url_to_repo = "invalid-url"

        def work(project: BackupProject) -> None:
            if project.id == 0:
                self.assertTrue(release_first.wait(5))
                raise RuntimeError("first failed")
            if project.id == 2:
                raise requests.exceptions.ReadTimeout(self.backup.gogs_token)

        def collect(futures: dict[Future[None], int]) -> Iterator[Future[None]]:
            for future in as_completed(futures, timeout=10):
                completions.append(futures[future])
                yield future
                if futures[future] == 2:
                    release_first.set()

        with (
            patch.object(self.backup, "_backup_repository", new=work),
            patch("main.as_completed", new=collect),
            self.assertLogs("main", level="INFO") as logs,
            self.assertRaises(SystemExit) as caught,
        ):
            self.backup.run()
        self.assertEqual(caught.exception.code, 1)
        self.assertLess(completions.index(2), completions.index(0))
        messages = "\n".join(logs.output)
        self.assertIn("Successful: 3 repositories", messages)
        self.assertIn("Failed: 3 repositories", messages)
        self.assertNotIn(self.backup.gogs_token, messages)
        failures = [r for r in logs.records if r.getMessage().startswith("  - ")]
        self.assertEqual(len(failures), 3)
        for idx, record in enumerate(failures):
            self.assertIn(f"survey{idx}", record.getMessage())
            self.assertEqual(record.thread, main_thread)
        request_errors = [
            r for r in logs.records if "GOGS API request failed" in r.getMessage()
        ]
        self.assertTrue(all(r.exc_info is None for r in request_errors))

    def test_retry_and_credentials_are_isolated_between_workers(self) -> None:
        self.backup.parallel_threads = 2
        self.backup.gl.groups.get.return_value.projects.list.return_value = (
            self.projects[:2]
        )
        retry_started = threading.Event()
        other_finished = threading.Event()
        barrier = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        environments = []
        request_headers: list[dict[str, str]] = []
        attempts = 0
        original_environment = dict(os.environ)
        original_headers = dict(self.backup.gogs_headers)

        def request(
            *,
            method: str,
            url: str,
            headers: dict[str, str],
            json: object,
            timeout: int,
        ) -> requests.Response:
            nonlocal attempts
            with lock:
                request_headers.append(headers)
                headers["worker-only"] = "test"
                if url.endswith("survey0"):
                    attempts += 1
                    if attempts == 1:
                        raise requests.exceptions.ReadTimeout("fixture")
            return response(200, {})

        def sleep(delay: int) -> None:
            self.assertEqual(delay, 2)
            retry_started.set()
            self.assertTrue(other_finished.wait(5))

        def work(project: BackupProject) -> None:
            with self.backup._git_credentials(project.name, "test-token") as env:
                with lock:
                    environments.append(env)
                barrier.wait()
                self.assertTrue(Path(env["GIT_ASKPASS"]).exists())
                self.assertEqual(env["BACKUP_GIT_USERNAME"], project.name)
                if project.id == 1:
                    self.assertTrue(retry_started.wait(5))
                self.backup._gogs_api_request("GET", f"/repos/test-org/{project.name}")
                if project.id == 1:
                    other_finished.set()

        with (
            patch.object(self.backup, "_backup_repository", new=work),
            patch("main.requests.request", new=request),
            patch("main.time.sleep", new=sleep),
            self.assertLogs("main", level="INFO"),
        ):
            self.backup.run()
        self.assertEqual(attempts, 2)
        self.assertEqual(len({id(h) for h in request_headers}), 3)
        self.assertEqual(self.backup.gogs_headers, original_headers)
        self.assertEqual(os.environ, original_environment)
        self.assertEqual(len({env["GIT_ASKPASS"] for env in environments}), 2)
        self.assertTrue(
            all(not Path(env["GIT_ASKPASS"]).exists() for env in environments)
        )
