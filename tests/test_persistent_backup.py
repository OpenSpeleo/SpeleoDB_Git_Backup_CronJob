"""Persistent mirror failure handling and concurrent-run protection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from git import GitCommandError
from git import Repo

from tests.test_backup import make_backup

# ruff: noqa: PT009, PT027, SLF001


class PersistentBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backup = make_backup()
        self.backup.backup_dir = Path(directory.name)
        self.project = SimpleNamespace(
            id=12,
            name="survey",
            http_url_to_repo="https://gitlab.test/group/survey.git",
        )

    def test_path_survives_rename_and_separates_instances_and_projects(self) -> None:
        original = self.backup._repository_path(self.project)
        self.project.name = "renamed"
        self.assertEqual(self.backup._repository_path(self.project), original)
        self.project.id = 13
        self.assertNotEqual(self.backup._repository_path(self.project), original)
        self.project.id = 12
        self.backup.gitlab_url = "https://other-gitlab.test"
        self.assertNotEqual(self.backup._repository_path(self.project), original)

    def test_lock_rejects_overlap_and_releases_after_failure(self) -> None:
        path = self.backup._repository_path(self.project)
        with (
            self.assertRaisesRegex(RuntimeError, "in use"),
            self.backup._repository_lock(path),
            self.backup._repository_lock(path),
        ):
            self.fail("Overlapping backups must not modify the same mirror")
        with self.backup._repository_lock(path):
            self.assertTrue(path.parent.exists())

    def test_failed_initial_clone_leaves_no_persistent_partial_repo(self) -> None:
        path = self.backup._repository_path(self.project)

        def fail_clone(url: str, clone_path: Path, **kwargs: object) -> None:
            clone_path.mkdir()
            (clone_path / "partial").write_text("incomplete")
            raise GitCommandError("git clone", 128, stderr="HTTP 503")

        with (
            self.backup._repository_lock(path),
            patch("main.Repo.clone_from", side_effect=fail_clone) as clone,
            patch("main.time.sleep"),
            self.assertRaises(GitCommandError),
        ):
            self.backup._clone_or_fetch_repository(self.project, path)
        self.assertEqual(clone.call_count, 6)
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.iterdir()), [path.with_suffix(".lock")])

    def test_nonbare_repository_is_preserved_and_not_pushed(self) -> None:
        path = self.backup._repository_path(self.project)
        with Repo.init(path):
            marker = path / "keep.txt"
            marker.write_text("keep")
        with (
            patch.object(self.backup, "_push_repository_with_retry") as push,
            self.assertRaisesRegex(ValueError, "must be bare"),
        ):
            self.backup._backup_repository(self.project)
        push.assert_not_called()
        self.assertEqual(marker.read_text(), "keep")

    def test_fetch_retries_transient_errors_and_refreshes_source_url(self) -> None:
        repo = MagicMock()
        repo.git.fetch.side_effect = [
            GitCommandError("git fetch", 128, stderr="HTTP 503"),
            GitCommandError("git fetch", 128, stderr="early EOF"),
            "",
        ]
        with patch("main.time.sleep") as sleep:
            self.backup._fetch_repository_with_retry(repo, self.project)
        self.assertEqual(repo.git.fetch.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        repo.remotes.origin.set_url.assert_called_once_with(
            self.project.http_url_to_repo
        )

    def test_fetch_retry_budget_and_auth_failure(self) -> None:
        for error, attempts in (("HTTP 503", 6), ("Authentication failed", 1)):
            repo = MagicMock()
            repo.git.fetch.side_effect = GitCommandError("git fetch", 128, stderr=error)
            with (
                self.subTest(error=error),
                patch("main.time.sleep") as sleep,
                self.assertRaises(GitCommandError),
            ):
                self.backup._fetch_repository_with_retry(repo, self.project)
            self.assertEqual(repo.git.fetch.call_count, attempts)
            self.assertEqual(sleep.call_count, attempts - 1)
