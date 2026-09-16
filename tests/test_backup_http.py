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
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from urllib.parse import urlsplit

from git import Repo

from tests.test_backup import make_backup

if TYPE_CHECKING:
    from main import GitLabToGOGSBackup

# The fixture executes the installed git binary against disposable local repositories.
# Tokens below are synthetic fixtures.
# ruff: noqa: PT009, PT027, S603, S105


class GitHTTPServer(ThreadingHTTPServer):
    root: Path
    source_token: str
    destination_token: str
    username: str
    created: int = 0


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
        if self.command == "GET":
            exists = (self.server.root / "test-org/survey.git").exists()
            self.respond(200 if exists else 404)
            return
        assert path == "/api/v1/org/test-org/repos"
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert data["name"] == "survey"
        with (
            Repo.init(self.server.root / "test-org/survey.git", bare=True) as repo,
            repo.config_writer() as config,
        ):
            config.set_value("http", "receivepack", "true")
        self.server.created += 1
        self.respond(201, b'{"name":"survey"}')


class HTTPBackupTests(unittest.TestCase):
    def test_authenticated_mirror_and_failure_reporting(self) -> None:
        backup = make_backup()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
                source.git.update_ref("-d", "refs/heads/feature")
                backup.run()
                with Repo(root / "test-org/survey.git") as destination:
                    self.assertEqual(source.git.show_ref(), destination.git.show_ref())
                self.assertEqual(server.created, 1)
                backup.gogs_token = "incorrect-test-token"
                with self.assertRaises(SystemExit) as caught:
                    backup.run()
                self.assertEqual(caught.exception.code, 1)
            output = "\n".join(logs.output)
            self.assertIn("Successfully backed up survey", output)
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
