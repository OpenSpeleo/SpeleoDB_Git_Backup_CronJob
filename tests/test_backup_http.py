"""Safe end-to-end backup check using temporary repos and a loopback HTTP server."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from unittest.mock import patch
from urllib.parse import urlsplit

from git import Repo

from tests.test_backup import make_backup

if TYPE_CHECKING:
    from main import BackupProject
    from main import GitLabToGOGSBackup

# The fixture executes the installed git binary against disposable local repositories.
# Tokens below are synthetic fixtures.
# ruff: noqa: PT009, PT027, S603, S105, SLF001


class GitHTTPServer(ThreadingHTTPServer):
    root: Path
    source_token: str
    destination_token: str
    username: str

    def __init__(
        self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]
    ) -> None:
        super().__init__(address, handler)
        self.created = 0
        self.lookup_failures_remaining = 1
        self.state_lock = threading.Lock()


class GitHTTPHandler(BaseHTTPRequestHandler):
    server: GitHTTPServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Suppress request logs; failed requests are asserted by the tests."""

    def do_GET(self) -> None:
        self.handle_request()

    def do_POST(self) -> None:
        self.handle_request()

    def respond(self, status: int, body: bytes = b"{}") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_request(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/v1/"):
            self.handle_api(parsed.path)
            return
        source = parsed.path.startswith("/source/")
        username = "oauth2" if source else self.server.username
        token = self.server.source_token if source else self.server.destination_token
        expected = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="backup-test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        git = shutil.which("git")
        assert git is not None
        response = subprocess.run(
            [git, "http-backend"],
            input=body,
            capture_output=True,
            check=True,
            timeout=30,
            env={
                **os.environ,
                "GIT_PROJECT_ROOT": str(self.server.root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": self.command,
                "PATH_INFO": parsed.path,
                "QUERY_STRING": parsed.query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "REMOTE_USER": username,
            },
        )
        raw_headers, payload = response.stdout.split(b"\r\n\r\n", 1)
        headers = dict(
            line.split(": ", 1) for line in raw_headers.decode().split("\r\n")
        )
        status = int(headers.pop("Status", "200 OK").split()[0])
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_api(self, path: str) -> None:
        if (
            self.headers.get("Authorization")
            != f"token {self.server.destination_token}"
        ):
            self.respond(401)
            return
        with self.server.state_lock:
            if self.command == "GET":
                if self.server.lookup_failures_remaining:
                    self.server.lookup_failures_remaining -= 1
                    self.respond(503)
                    return
                name = path.rsplit("/", 1)[-1]
                exists = (self.server.root / "test-org" / f"{name}.git").exists()
                self.respond(200 if exists else 404)
                return
            assert path == "/api/v1/org/test-org/repos"
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            name = data["name"]
            assert "/" not in name
            assert ".." not in name
            with (
                Repo.init(
                    self.server.root / "test-org" / f"{name}.git", bare=True
                ) as repo,
                repo.config_writer() as config,
            ):
                config.set_value("http", "receivepack", "true")
            self.server.created += 1
            self.respond(201, json.dumps({"name": name}).encode())


class HTTPBackupTests(unittest.TestCase):
    def test_parallel_initial_and_incremental_mirrors(self) -> None:
        backup = make_backup()
        backup.parallel_threads = 2
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            backup.backup_dir = root / "mirrors"
            server = GitHTTPServer(("127.0.0.1", 0), GitHTTPHandler)
            server.root = root
            server.source_token = backup.gitlab_token
            server.destination_token = backup.gogs_token
            server.username = backup.gogs_username
            server.lookup_failures_remaining = 0
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join, timeout=5)
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            backup.gogs_url = f"http://127.0.0.1:{server.server_port}"
            sources = []
            projects = []
            for idx in range(4):
                name = f"00000000-0000-0000-0000-{idx:012d}"
                source = stack.enter_context(
                    Repo.init(root / "source" / f"{name}.git", bare=True)
                )
                tree = source.git.hash_object("-t", "tree", "--stdin", istream=None)
                with source.git.custom_environment(
                    GIT_AUTHOR_NAME="Test",
                    GIT_AUTHOR_EMAIL="test@example.invalid",
                    GIT_COMMITTER_NAME="Test",
                    GIT_COMMITTER_EMAIL="test@example.invalid",
                ):
                    commit = source.git.commit_tree(tree, m=name)
                source.git.update_ref("refs/heads/main", commit)
                source.git.update_ref("refs/tags/v1", commit)
                source.git.symbolic_ref("HEAD", "refs/heads/main")
                sources.append(source)
                projects.append(
                    SimpleNamespace(
                        id=idx,
                        name=name,
                        path_with_namespace=f"test-group/{name}",
                        description="Parallel HTTP fixture",
                        visibility="private",
                        http_url_to_repo=f"{backup.gogs_url}/source/{name}.git",
                    )
                )
            backup.gl = MagicMock()
            backup.gl.groups.get.return_value.projects.list.return_value = projects
            backup.gl.projects.get.side_effect = lambda idx: projects[idx]
            barrier = threading.Barrier(2, timeout=10)
            handles = []
            handles_lock = threading.Lock()
            clone_or_fetch = backup._clone_or_fetch_repository

            def synchronized_transfer(project: BackupProject, path: Path) -> Repo:
                barrier.wait()
                repo = clone_or_fetch(project, path)
                with handles_lock:
                    handles.append(repo)
                return repo

            original_environment = dict(os.environ)
            with self.assertLogs("main", level="INFO") as logs:
                for incremental in (False, True):
                    if incremental:
                        for source in sources:
                            source.git.update_ref(
                                "refs/heads/new", source.head.commit.hexsha
                            )
                            source.git.update_ref("-d", "refs/tags/v1")
                    with ExitStack() as phase:
                        phase.enter_context(
                            patch.object(
                                backup,
                                "_clone_or_fetch_repository",
                                new=synchronized_transfer,
                            )
                        )
                        if incremental:
                            phase.enter_context(
                                patch(
                                    "main.Repo.clone_from",
                                    side_effect=AssertionError("Unexpected reclone"),
                                )
                            )
                        backup.run()
                    for project, source in zip(projects, sources, strict=True):
                        with (
                            Repo(
                                root / "test-org" / f"{project.name}.git"
                            ) as destination,
                            Repo(backup._repository_path(project)) as mirror,
                        ):
                            self.assertEqual(
                                source.git.show_ref(), destination.git.show_ref()
                            )
                            self.assertEqual(
                                source.git.show_ref(), mirror.git.show_ref()
                            )
                            config = Path(mirror.git_dir, "config").read_text()
                            self.assertNotIn(backup.gitlab_token, config)
                            self.assertNotIn(backup.gogs_token, config)
                # One failed fetch cannot prevent the other parallel backups.
                projects[0].http_url_to_repo = f"{backup.gogs_url}/source/missing.git"
                with self.assertRaises(SystemExit) as caught:
                    backup.run()
                self.assertEqual(caught.exception.code, 1)
            output = "\n".join(logs.output)
            self.assertIn("Successful: 4 repositories", output)
            self.assertIn("Successful: 3 repositories", output)
            self.assertIn("Failed: 1 repositories", output)
            self.assertNotIn(backup.gitlab_token, output)
            self.assertNotIn(backup.gogs_token, output)
            self.assertEqual(server.created, 4)
            self.assertEqual(len({id(repo) for repo in handles}), 8)
            self.assertEqual(os.environ, original_environment)

    def test_authenticated_mirror_and_failure_reporting(self) -> None:
        backup = make_backup()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup.backup_dir = root / "mirrors"
            with Repo.init(root / "source/survey.git", bare=True) as source:
                commit = source.git.hash_object("-t", "tree", "--stdin", istream=None)
                # Create a real commit without relying on global Git identity.
                with source.git.custom_environment(
                    GIT_AUTHOR_NAME="Test",
                    GIT_AUTHOR_EMAIL="test@example.invalid",
                    GIT_COMMITTER_NAME="Test",
                    GIT_COMMITTER_EMAIL="test@example.invalid",
                ):
                    commit = source.git.commit_tree(commit, m="Backup fixture")
                source.git.update_ref("refs/heads/main", commit)
                source.git.update_ref("refs/heads/feature", commit)
                source.git.update_ref("refs/tags/v1", commit)
                source.git.update_ref("refs/tags/delete-me", commit)
                source.git.update_ref("refs/notes/test", commit)
                source.git.symbolic_ref("HEAD", "refs/heads/main")
                self.check_backup(backup, root, source)

    def check_backup(
        self, backup: GitLabToGOGSBackup, root: Path, source: Repo
    ) -> None:
        server = GitHTTPServer(("127.0.0.1", 0), GitHTTPHandler)
        server.root = root
        server.source_token = backup.gitlab_token
        server.destination_token = backup.gogs_token
        server.username = backup.gogs_username
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backup.gogs_url = f"http://127.0.0.1:{server.server_port}"
            project = SimpleNamespace(
                id=1,
                name="survey",
                path_with_namespace="test-group/survey",
                description="HTTP authentication regression fixture",
                visibility="private",
                http_url_to_repo=f"{backup.gogs_url}/source/survey.git",
            )
            backup.gl = MagicMock()
            backup.gl.groups.get.return_value.projects.list.return_value = [project]
            backup.gl.projects.get.return_value = project
            with self.assertLogs("main", level="INFO") as logs:
                try:
                    backup.run()
                except SystemExit:
                    self.fail("\n".join(logs.output))
                with Repo(root / "test-org/survey.git") as destination:
                    self.assertEqual(source.git.show_ref(), destination.git.show_ref())
                mirror_path = backup._repository_path(project)
                with Repo(mirror_path) as mirror:
                    self.assertTrue(mirror.bare)
                    self.assertEqual(
                        mirror.remotes.origin.url, project.http_url_to_repo
                    )
                    self.assertNotIn(
                        server.source_token, Path(mirror.git_dir, "config").read_text()
                    )
                    self.assertNotIn(
                        server.destination_token,
                        Path(mirror.git_dir, "config").read_text(),
                    )
                    mirror.git.update_ref(
                        "refs/heads/local-only", source.head.commit.hexsha
                    )
                # A new root commit simulates a non-fast-forward upstream rewrite.
                with source.git.custom_environment(
                    GIT_AUTHOR_NAME="Test",
                    GIT_AUTHOR_EMAIL="test@example.invalid",
                    GIT_COMMITTER_NAME="Test",
                    GIT_COMMITTER_EMAIL="test@example.invalid",
                ):
                    rewritten = source.git.commit_tree(
                        source.head.commit.tree.hexsha, m="Rewritten history"
                    )
                source.git.update_ref("refs/heads/main", rewritten)
                source.git.update_ref("refs/tags/v1", rewritten)
                source.git.update_ref("-d", "refs/heads/feature")
                source.git.update_ref("-d", "refs/tags/delete-me")
                source.git.update_ref("-d", "refs/notes/test")
                with Repo(root / "test-org/survey.git") as destination:
                    destination.git.update_ref(
                        "refs/heads/destination-only",
                        destination.heads.main.commit.hexsha,
                    )
                # Reconstruct the orchestrator to emulate a separate cron invocation.
                restarted = make_backup()
                restarted.__dict__.update(backup.__dict__)
                with patch(
                    "main.Repo.clone_from",
                    side_effect=AssertionError("Unexpected reclone"),
                ):
                    restarted.run()
                with Repo(mirror_path) as mirror:
                    self.assertEqual(source.git.show_ref(), mirror.git.show_ref())
                with Repo(root / "test-org/survey.git") as destination:
                    self.assertEqual(source.git.show_ref(), destination.git.show_ref())
                self.assertEqual(server.created, 1)
                # A failed fetch must not push stale refs or discard the local mirror.
                backup.gitlab_token = "incorrect-source-token"
                with (
                    patch.object(backup, "_push_repository_with_retry") as push,
                    self.assertRaises(SystemExit),
                ):
                    backup.run()
                push.assert_not_called()
                with Repo(mirror_path) as mirror:
                    self.assertEqual(source.git.show_ref(), mirror.git.show_ref())
                backup.gitlab_token = server.source_token
                backup.gogs_token = "incorrect-test-token"
                with self.assertRaises(SystemExit) as caught:
                    backup.run()
                self.assertEqual(caught.exception.code, 1)
            output = "\n".join(logs.output)
            self.assertIn("Successfully backed up survey", output)
            self.assertIn("Retrying in 2 seconds", output)
            self.assertIn("Failed: 1 repositories", output)
            self.assertNotIn(server.source_token, output)
            self.assertNotIn(server.destination_token, output)
            self.assertNotIn(backup.gogs_token, output)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
