"""Exercise the real GitLab client with a controlled HTTP transport."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock
from unittest.mock import patch

import gitlab
import requests

from main import RetryingGitlab
from main import _is_retryable_gitlab_error
from main import _retry_operation
from tests.test_backup import make_backup

# Standard-library unittest assertions; no production credentials are used.
# ruff: noqa: PT009, PT027, SLF001


def response(
    status: int, data: object, *, next_page: bool = False
) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    result.headers["Content-Type"] = "application/json"
    if next_page:
        result.headers["Link"] = (
            '<https://gitlab.test/api/v4/groups/1/projects?page=2>; rel="next"'
        )
    return result


class GitLabRetryTests(unittest.TestCase):
    def test_auth_group_listing_pages_and_project_details_retry(self) -> None:
        client = RetryingGitlab("https://gitlab.test", timeout=30)
        responses = [
            response(503, {}),
            response(200, {"id": 1}),  # auth
            response(502, {}),
            response(200, {"id": 1, "name": "group"}),
            response(429, {}),
            response(200, [{"id": 11}], next_page=True),
            response(504, {}),
            response(200, [{"id": 12}]),
            response(500, {}),
            response(200, {"id": 11, "name": "survey"}),
        ]
        with (
            patch.object(client.session, "request", side_effect=responses) as request,
            patch("main.time.sleep") as sleep,
        ):
            client.auth()
            group = client.groups.get(1)
            projects = group.projects.list(
                all=True, include_subgroups=True, archived=False
            )
            project = client.projects.get(11)
        self.assertEqual([item.id for item in projects], [11, 12])
        self.assertEqual(project.name, "survey")
        self.assertEqual(request.call_count, 10)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1] * 5)
        # Retry the failed second page without replaying the first page.
        for call in request.call_args_list[6:8]:
            self.assertEqual(call.kwargs["params"]["page"], ["2"])

    def test_http_failures_have_exactly_five_retries(self) -> None:
        for status in (408, 429, 500, 502, 503, 504, 520, 524):
            with self.subTest(status=status):
                client = RetryingGitlab("https://gitlab.test")
                reply = response(status, {"message": "temporarily unavailable"})
                reply.headers["Retry-After"] = "120"
                with (
                    patch.object(
                        client.session, "request", return_value=reply
                    ) as request,
                    patch("main.time.sleep") as sleep,
                    self.assertRaises(gitlab.GitlabError),
                ):
                    client.groups.get(1)
                self.assertEqual(request.call_count, 6)
                self.assertEqual(
                    [call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8, 16]
                )

    def test_transport_failures_have_exactly_five_retries(self) -> None:
        for error in (
            requests.ConnectionError("disconnected"),
            requests.Timeout("timeout"),
            requests.exceptions.ChunkedEncodingError("interrupted response"),
        ):
            with self.subTest(error=type(error).__name__):
                client = RetryingGitlab("https://gitlab.test")
                with (
                    patch.object(
                        client.session, "request", side_effect=error
                    ) as request,
                    patch("main.time.sleep") as sleep,
                    self.assertRaises(type(error)),
                ):
                    client.auth()
                self.assertEqual(request.call_count, 6)
                self.assertEqual(sleep.call_count, 5)

    def test_permanent_errors_are_not_retried(self) -> None:
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                client = RetryingGitlab("https://gitlab.test")
                with (
                    patch.object(
                        client.session, "request", return_value=response(status, {})
                    ) as request,
                    patch("main.time.sleep") as sleep,
                    self.assertRaises(gitlab.GitlabError),
                ):
                    client.groups.get(1)
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()

    def test_last_retry_can_succeed(self) -> None:
        client = RetryingGitlab("https://gitlab.test")
        replies = [response(503, {}) for _ in range(5)] + [response(200, {"id": 1})]
        with (
            patch.object(client.session, "request", side_effect=replies) as request,
            patch("main.time.sleep") as sleep,
        ):
            client.auth()
        self.assertEqual(client.user.id, 1)
        self.assertEqual(request.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    def test_project_failure_does_not_add_another_retry_loop(self) -> None:
        backup = make_backup()
        backup.gl = RetryingGitlab("https://gitlab.test")
        with (
            patch.object(
                backup.gl.session, "request", return_value=response(503, {})
            ) as request,
            patch("main.time.sleep") as sleep,
            self.assertLogs("main", level="WARNING"),
        ):
            self.assertIsNone(backup._get_full_project_with_retry(1, "group/survey"))
        self.assertEqual(request.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    def test_excessive_retry_budget_is_rejected(self) -> None:
        operation = Mock()
        with self.assertRaises(ValueError):
            _retry_operation(operation, "test", _is_retryable_gitlab_error, retries=6)
        operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
