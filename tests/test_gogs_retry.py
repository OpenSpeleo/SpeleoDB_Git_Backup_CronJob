"""GOGS retry and project failure isolation regressions."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import requests

from main import TRANSIENT_HTTP_STATUSES
from tests.test_backup import make_backup
from tests.test_gitlab_retry import response

# Exercise internal operations using synthetic responses and no live credentials.
# ruff: noqa: PT009, PT027, SLF001


class GOGSRetryTests(unittest.TestCase):
    def test_transient_failures_can_recover_on_last_attempt(self) -> None:
        failures = [
            requests.exceptions.ReadTimeout("timed out"),
            requests.exceptions.ConnectTimeout("timed out"),
            requests.ConnectionError("disconnected"),
            requests.exceptions.ChunkedEncodingError("interrupted response"),
            *(response(status, {}) for status in sorted(TRANSIENT_HTTP_STATUSES)),
        ]
        for failure in failures:
            with (
                self.subTest(failure=failure),
                patch(
                    "main.requests.request",
                    side_effect=[failure] * 5 + [response(200, {})],
                ) as request,
                patch("main.time.sleep") as sleep,
            ):
                self.assertTrue(make_backup()._check_gogs_repo_exists("survey"))
                self.assertEqual(request.call_count, 6)
                self.assertEqual(
                    [call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8, 16]
                )
                self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_permanent_errors_are_not_retried(self) -> None:
        for status in (400, 401, 403, 404, 409, 422):
            with (
                self.subTest(status=status),
                patch(
                    "main.requests.request", return_value=response(status, {})
                ) as request,
                patch("main.time.sleep") as sleep,
                self.assertRaises(requests.HTTPError),
            ):
                try:
                    make_backup()._gogs_api_request("GET", "/repos/test-org/survey")
                finally:
                    self.assertEqual(request.call_count, 1)
                    sleep.assert_not_called()

    def test_certificate_failure_is_not_retried(self) -> None:
        with (
            patch(
                "main.requests.request",
                side_effect=requests.exceptions.SSLError("invalid certificate"),
            ) as request,
            patch("main.time.sleep") as sleep,
            self.assertRaises(requests.exceptions.SSLError),
        ):
            try:
                make_backup()._check_gogs_repo_exists("survey")
            finally:
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()

    def test_missing_repository_still_returns_false(self) -> None:
        with (
            patch("main.requests.request", return_value=response(404, {})) as request,
            patch("main.time.sleep") as sleep,
        ):
            self.assertFalse(make_backup()._check_gogs_repo_exists("survey"))
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_creation_timeout_then_conflict_is_successful(self) -> None:
        project = SimpleNamespace(name="survey", description="", visibility="private")
        for org in ("test-org", ""):
            backup = make_backup()
            backup.gogs_org = org
            with (
                self.subTest(org=org),
                patch(
                    "main.requests.request",
                    side_effect=[requests.exceptions.ReadTimeout(), response(409, {})],
                ) as request,
                patch("main.time.sleep") as sleep,
            ):
                self.assertEqual(backup._create_gogs_repo(project), {"name": "survey"})
                self.assertEqual(request.call_count, 2)
                self.assertEqual(request.call_args.kwargs["method"], "POST")
                sleep.assert_called_once_with(1)

    def test_org_verification_retries(self) -> None:
        with (
            patch(
                "main.requests.request",
                side_effect=[response(503, {}), response(200, {})],
            ) as request,
            patch("main.time.sleep") as sleep,
        ):
            make_backup()._verify_gogs_org_exists()
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_exhaustion_skips_project_without_traceback_and_cleans_up(self) -> None:
        backup = make_backup()
        projects = [
            SimpleNamespace(id=i, name=f"survey{i}", path_with_namespace=f"group/s{i}")
            for i in range(2)
        ]
        backup.gl = MagicMock()
        backup.gl.groups.get.return_value.projects.list.return_value = projects
        for failure in (
            requests.exceptions.ReadTimeout(backup.gogs_token),
            response(503, {}),
        ):
            repos = [MagicMock(), MagicMock()]
            backup.gl.projects.get.side_effect = projects
            with (
                self.subTest(failure=failure),
                patch.object(backup, "_clone_repository_with_retry", side_effect=repos),
                patch.object(backup, "_push_repository_with_retry") as push,
                patch(
                    "main.requests.request",
                    side_effect=[failure] * 6 + [response(200, {})],
                ) as request,
                patch("main.time.sleep") as sleep,
                self.assertLogs("main", level="INFO") as logs,
                self.assertRaises(SystemExit) as caught,
            ):
                backup.run()
            self.assertEqual(caught.exception.code, 1)
            self.assertEqual(request.call_count, 7)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8, 16]
            )
            push.assert_called_once_with(repos[1].create_remote.return_value, "survey1")
            for repo in repos:
                repo.close.assert_called_once_with()
            output = "\n".join(logs.output)
            self.assertIn("Skipping project", output)
            self.assertIn("Successful: 1 repositories", output)
            self.assertIn("Failed: 1 repositories", output)
            self.assertNotIn("Traceback", output)
            self.assertNotIn(backup.gogs_token, output)
            self.assertTrue(all(record.exc_info is None for record in logs.records))


if __name__ == "__main__":
    unittest.main()
