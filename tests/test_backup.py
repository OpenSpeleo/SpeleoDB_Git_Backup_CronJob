"""Regression tests; run with `uv run python -m unittest discover -v`."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from git import GitCommandError
from git import Repo
from git.remote import PushInfoList

from main import GitLabToGOGSBackup

# Test the internal backup operations without production credentials or network I/O.
# Tokens below are synthetic fixtures.
# ruff: noqa: SLF001, PT009, PT027, S105, S106


def make_backup() -> GitLabToGOGSBackup:
    backup = GitLabToGOGSBackup.__new__(GitLabToGOGSBackup)
    backup.gitlab_url = "https://gitlab.com"
    backup.gitlab_token = "source-test-token"
    backup.gitlab_group_id = "1"
    backup.gogs_url = "http://gogs.railway.internal:3000"
    backup.gogs_username = "backup-user"
    backup.gogs_token = "test-token:@/% ?&'\"$"
    backup.gogs_org = "test-org"
    backup.gogs_headers = {"Authorization": f"token {backup.gogs_token}"}
    return backup


class BackupTests(unittest.TestCase):
    def test_credentials_are_not_written_to_disk_and_helper_is_removed(self) -> None:
        backup = make_backup()
        with backup._git_credentials(backup.gogs_username, backup.gogs_token) as env:
            helper = Path(env["GIT_ASKPASS"])
            self.assertNotIn(backup.gogs_token, helper.read_text())
            self.assertNotIn(backup.gogs_username, helper.read_text())
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(helper.stat().st_mode & 0o777, 0o700)
        self.assertFalse(helper.exists())

    def test_gogs_urls_preserve_transport_port_and_base_path(self) -> None:
        backup = make_backup()
        for base in ("http://gogs.railway.internal:3000", "https://gogs.test/base"):
            for owner in ("test-org", ""):
                with self.subTest(base=base, owner=owner):
                    backup.gogs_url = base
                    backup.gogs_org = owner
                    self.assertEqual(
                        backup._get_gogs_clone_url("survey name"),
                        f"{base}/{owner or backup.gogs_username}/survey%20name.git",
                    )

    def test_gitlab_accepts_host_or_explicit_url(self) -> None:
        for host, expected in (
            ("gitlab.com", "https://gitlab.com"),
            ("https://gitlab.com/", "https://gitlab.com"),
            (
                "http://gitlab.internal:8080/gitlab/",
                "http://gitlab.internal:8080/gitlab",
            ),
        ):
            with (
                self.subTest(host=host),
                patch.dict(
                    os.environ,
                    {
                        "GITLAB_HOST_URL": host,
                        "GITLAB_TOKEN": "source-test-token",
                        "GITLAB_GROUP_ID": "1",
                        "GOGS_INSTANCE_URL": "http://gogs.internal:3000/",
                        "GOGS_USERNAME": "backup-user",
                        "GOGS_ACCESS_TOKEN": "destination-test-token",
                        "GOGS_ORG": "",
                    },
                    clear=True,
                ),
                patch("main.gitlab.Gitlab") as client,
            ):
                backup = GitLabToGOGSBackup()
                self.assertEqual(backup.gitlab_url, expected)
                client.assert_called_once_with(
                    expected, private_token="source-test-token"
                )

    def test_rejects_invalid_urls_without_echoing_secrets(self) -> None:
        backup = make_backup()
        for url in (
            "gogs.internal:3000",
            "ftp://gogs.test",
            "http://",
            "http://gogs.test:bad",
            "https://user:secret@gogs.test",
            "https://gogs.test/?token=secret",
            "https://gogs.test/#secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                backup._validate_http_url(url, "GOGS_INSTANCE_URL")
            self.assertNotIn("secret", str(caught.exception))

    def test_missing_gogs_url_fails_before_network_calls(self) -> None:
        backup = make_backup()
        backup.gogs_url = ""
        with self.assertRaisesRegex(ValueError, "GOGS_INSTANCE_URL"):
            backup._validate_config()

    def test_transient_push_retries_with_backoff(self) -> None:
        backup = make_backup()
        origin = MagicMock()
        origin.push.side_effect = [
            GitCommandError("git push", 128, stderr="HTTP 503"),
            GitCommandError("git push", 128, stderr="connection reset"),
            PushInfoList(),
        ]
        with patch("main.time.sleep") as sleep:
            backup._push_repository_with_retry(origin, "survey", retries=2)
        self.assertEqual(origin.push.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_push_rejection_and_auth_failure_are_terminal(self) -> None:
        backup = make_backup()
        for message in ("Authentication failed", "pre-receive hook declined"):
            with self.subTest(message=message):
                origin = MagicMock()
                result = PushInfoList()
                result.error = GitCommandError("git push", 1, stderr=message)
                origin.push.return_value = result
                with (
                    patch("main.time.sleep") as sleep,
                    self.assertRaises(GitCommandError),
                ):
                    backup._push_repository_with_retry(origin, "survey")
                self.assertEqual(origin.push.call_count, 1)
                sleep.assert_not_called()

    def test_push_stops_after_retry_budget(self) -> None:
        origin = MagicMock()
        origin.push.side_effect = GitCommandError("git push", 128, stderr="HTTP 502")
        with patch("main.time.sleep"), self.assertRaises(GitCommandError):
            make_backup()._push_repository_with_retry(origin, "survey", retries=2)
        self.assertEqual(origin.push.call_count, 3)

    def test_clone_retry_removes_partial_repository(self) -> None:
        backup = make_backup()
        project = SimpleNamespace(
            name="survey", http_url_to_repo="https://gitlab.test/r"
        )
        attempts = []

        def clone(url: str, path: str, **kwargs: object) -> Repo:
            attempts.append(url)
            self.assertFalse(Path(path).exists())
            if len(attempts) == 1:
                Path(path).mkdir()
                raise GitCommandError("git clone", 128, stderr="early EOF")
            return MagicMock(spec=Repo)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("main.Repo.clone_from", side_effect=clone),
            patch("main.time.sleep"),
        ):
            backup._clone_repository_with_retry(project, directory, retries=1)
        self.assertEqual(attempts, [project.http_url_to_repo] * 2)

    def test_run_continues_after_failure_and_exits_nonzero(self) -> None:
        backup = make_backup()
        projects = [
            SimpleNamespace(
                id=i, name=f"survey{i}", path_with_namespace=f"group/survey{i}"
            )
            for i in range(2)
        ]
        backup.gl = MagicMock()
        backup.gl.groups.get.return_value.projects.list.return_value = projects
        backup.gl.projects.get.side_effect = projects
        with (
            patch.object(
                backup, "_backup_repository", side_effect=[RuntimeError("bad"), None]
            ) as run,
            self.assertLogs("main", level="INFO") as logs,
            self.assertRaises(SystemExit) as caught,
        ):
            backup.run()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(run.call_count, 2)
        self.assertIn("Successful: 1 repositories", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
