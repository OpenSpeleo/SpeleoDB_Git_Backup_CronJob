"""
GitLab to GOGS Backup Script

This script backs up all repositories from a GitLab organization to GOGS.
It reads credentials from environment variables and uses GitPython for git operations.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit

import gitlab
import requests
from dotenv import load_dotenv
from git import GitCommandError
from git import Remote
from git import Repo

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

# Configure logging
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


MAX_RETRIES = 5
TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def _retry_operation[T](
    operation: Callable[[], T],
    description: str,
    is_retryable: Callable[[Exception], bool],
    retries: int = MAX_RETRIES,
) -> T:
    """Run once and retry transient failures at most five times."""
    if not 0 <= retries <= MAX_RETRIES:
        raise ValueError(f"retries must be between 0 and {MAX_RETRIES}")
    for attempt in range(retries + 1):
        try:
            return operation()
        except (
            GitCommandError,
            gitlab.GitlabError,
            requests.exceptions.RequestException,
        ) as error:
            if not is_retryable(error) or attempt == retries:
                # The caller logs the terminal exception once with its context.
                raise
            delay_seconds = 2**attempt
            logger.warning(
                "%s failed transiently (attempt %s/%s). Retrying in %s seconds...",
                description,
                attempt + 1,
                retries + 1,
                delay_seconds,
            )
            time.sleep(delay_seconds)
    raise RuntimeError("Unexpected retry state")


def _is_retryable_gitlab_error(error: Exception) -> bool:
    """Retry temporary HTTP/transport failures, never auth or invalid requests."""
    if isinstance(error, gitlab.GitlabError):
        return error.response_code in TRANSIENT_HTTP_STATUSES or isinstance(
            error, gitlab.GitlabConnectionError
        )
    if isinstance(error, requests.exceptions.HTTPError):
        return (
            error.response is not None
            and error.response.status_code in TRANSIENT_HTTP_STATUSES
        )
    if isinstance(error, requests.exceptions.SSLError):
        return False
    return isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    )


class RetryingGitlab(gitlab.Gitlab):
    """Apply one bounded backoff policy to every GitLab request, including pages."""

    def http_request(self, verb: str, path: str, **kwargs: Any) -> requests.Response:
        # Disable SDK retries so they cannot multiply our retry budget.
        kwargs.update(
            obey_rate_limit=False, retry_transient_errors=False, max_retries=0
        )
        request = super().http_request
        return _retry_operation(
            lambda: request(verb, path, **kwargs),
            f"GitLab {verb.upper()} request",
            _is_retryable_gitlab_error,
        )


class GitLabToGOGSBackup:
    """Handles backing up GitLab repositories to GOGS."""

    def __init__(self):
        """Initialize with credentials from environment variables."""
        # GitLab configuration
        self.gitlab_url = os.environ.get("GITLAB_HOST_URL", "gitlab.com").strip()
        if "://" not in self.gitlab_url:
            self.gitlab_url = f"https://{self.gitlab_url}"
        self.gitlab_token = os.environ.get("GITLAB_TOKEN", "")
        self.gitlab_group_id = os.environ.get("GITLAB_GROUP_ID", "")

        # GOGS configuration
        self.gogs_url = os.environ.get("GOGS_INSTANCE_URL", "")
        self.gogs_username = os.environ.get("GOGS_USERNAME", "")
        self.gogs_token = os.environ.get("GOGS_ACCESS_TOKEN", "")
        self.gogs_org = os.environ.get("GOGS_ORG", "")  # Optional: organization name

        # Validate required environment variables
        self._validate_config()

        # Log configuration (mask sensitive data)
        logger.info(f"GitLab URL: {self.gitlab_url}")
        logger.info(f"GitLab Group ID: {self.gitlab_group_id}")
        logger.info(f"GOGS URL: {self.gogs_url}")
        logger.info(f"GOGS Username: {self.gogs_username}")
        logger.info(
            f"GOGS Organization: '{self.gogs_org}' (empty means personal repos)"
        )

        # Initialize GitLab client
        self.gl = RetryingGitlab(
            self.gitlab_url, private_token=self.gitlab_token, timeout=30
        )
        self.gl.auth()

        # GOGS API headers
        self.gogs_headers = {
            "Authorization": f"token {self.gogs_token}",
            "Content-Type": "application/json",
        }

        # Verify GOGS organization exists if specified
        if self.gogs_org:
            self._verify_gogs_org_exists()

    def _get_full_project_with_retry(
        self, project_id: int, project_display_name: str
    ) -> Any | None:
        """Fetch project details; the GitLab client owns the request retry budget."""
        try:
            return self.gl.projects.get(project_id)
        except gitlab.GitlabError, requests.exceptions.RequestException:
            logger.exception(
                "Failed to load project details for '%s' (id=%s). Skipping project.",
                project_display_name,
                project_id,
            )
            return None

    def _is_retryable_git_error(self, error: Exception) -> bool:
        """Determine whether a Git transport error appears transient."""
        if not isinstance(error, GitCommandError):
            return False
        error_blob = " ".join(
            str(part)
            for part in (
                getattr(error, "stderr", ""),
                getattr(error, "stdout", ""),
                str(error),
            )
        ).lower()

        if any(
            marker in error_blob
            for marker in (
                "authentication failed",
                "could not read username",
                "access denied",
                "permission denied",
                "requested url returned error: 401",
                "requested url returned error: 403",
                "hook declined",
            )
        ):
            return False

        transient_markers = (
            "rpc failed",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "http 429",
            "requested url returned error: 500",
            "requested url returned error: 502",
            "requested url returned error: 503",
            "requested url returned error: 504",
            "requested url returned error: 429",
            "could not resolve host",
            "failed to connect",
            "remote end closed connection",
            "remote end hung up unexpectedly",
            "expected flush after ref listing",
            "connection reset",
            "connection timed out",
            "operation timed out",
            "tls handshake timeout",
            "early eof",
        )
        return any(marker in error_blob for marker in transient_markers)

    @staticmethod
    def _validate_http_url(url: str, setting: str) -> str:
        """Validate without including potentially sensitive input in errors."""
        url = url.strip().rstrip("/")
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not any(char.isspace() for char in url)
            )
            _ = parsed.port  # Validate a supplied port.
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                f"{setting} must be an HTTP(S) URL without credentials, "
                "query parameters, or a fragment"
            )
        return url

    @staticmethod
    @contextmanager
    def _git_credentials(username: str, token: str) -> Iterator[dict[str, str]]:
        """Provide credentials to Git without embedding them in URLs or files."""
        with tempfile.TemporaryDirectory(prefix="backup-askpass-") as directory:
            script = Path(directory) / "askpass.sh"
            script.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  Username*) printf "%s\\n" "$BACKUP_GIT_USERNAME" ;;\n'
                '  Password*) printf "%s\\n" "$BACKUP_GIT_TOKEN" ;;\n'
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            yield {
                "GIT_ASKPASS": str(script),
                "GIT_TERMINAL_PROMPT": "0",
                "BACKUP_GIT_USERNAME": username,
                "BACKUP_GIT_TOKEN": token,
                "LC_ALL": "C",
                # Disable credential helpers so they cannot cache the tokens.
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
            }

    def _clone_repository_with_retry(
        self, project: Any, destination_dir: str, retries: int = MAX_RETRIES
    ) -> Repo:
        """Clone a GitLab repository with exponential backoff on transient failures."""
        clone_path = Path(destination_dir) / "mirror-repo.git"
        gitlab_url = self._validate_http_url(
            project.http_url_to_repo, "GitLab repository URL"
        )

        def clone() -> Repo:
            # A failed clone may leave a partial repository behind.
            shutil.rmtree(clone_path, ignore_errors=True)
            with self._git_credentials("oauth2", self.gitlab_token) as env:
                repo = Repo.clone_from(gitlab_url, clone_path, mirror=True, env=env)
                # GitPython retains clone environment values on the new Repo.
                repo.git.update_environment(**dict.fromkeys(env))
                return repo

        return _retry_operation(
            clone, f"Git clone of {project.name}", self._is_retryable_git_error, retries
        )

    def _validate_config(self):
        """Validate that all required environment variables are set."""
        required_vars = {
            "GITLAB_TOKEN": self.gitlab_token,
            "GITLAB_GROUP_ID": self.gitlab_group_id,
            "GOGS_INSTANCE_URL": self.gogs_url,
            "GOGS_USERNAME": self.gogs_username,
            "GOGS_ACCESS_TOKEN": self.gogs_token,
        }

        missing_vars = [var for var, value in required_vars.items() if not value]
        if missing_vars:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing_vars)}"
            )

        self.gitlab_url = self._validate_http_url(self.gitlab_url, "GITLAB_HOST_URL")
        self.gogs_url = self._validate_http_url(self.gogs_url, "GOGS_INSTANCE_URL")

    def _verify_gogs_org_exists(self):
        """Verify that the GOGS organization exists and is accessible."""
        try:
            endpoint = f"/orgs/{self.gogs_org}"
            self._gogs_api_request("GET", endpoint)
            logger.info(f"Verified organization '{self.gogs_org}' exists in GOGS")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise ValueError(
                    f"Organization '{self.gogs_org}' not found in GOGS.  Please create "
                    "the organization first or check the organization name."
                ) from e

            if e.response.status_code == 403:
                raise ValueError(
                    f"Access denied to organization '{self.gogs_org}'. Please ensure "
                    "your token has permission to access this organization."
                ) from e

            raise

    def _gogs_api_request(
        self, method: str, endpoint: str, data: dict[str, Any] | None = None
    ) -> requests.Response:
        """Make a request to the GOGS API."""
        url = f"{self.gogs_url}/api/v1{endpoint}"
        response = requests.request(
            method=method, url=url, headers=self.gogs_headers, json=data, timeout=30
        )
        response.raise_for_status()
        return response

    def _check_gogs_repo_exists(self, repo_name: str) -> bool:
        """Check if a repository exists in GOGS."""
        try:
            if self.gogs_org:
                endpoint = f"/repos/{self.gogs_org}/{repo_name}"
            else:
                endpoint = f"/repos/{self.gogs_username}/{repo_name}"

            logger.debug(f"Checking if repo exists at: {endpoint}")
            _ = self._gogs_api_request("GET", endpoint)
            return True  # noqa: TRY300

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return False
            logger.exception("GOGS API request failed")
            raise

    def _create_gogs_repo(self, project: Any) -> dict[str, Any]:
        """Create a repository in GOGS."""
        repo_data = {
            "name": project.name,
            "description": project.description or "",
            "private": project.visibility != "public",
        }

        # Create in organization if specified
        if self.gogs_org:
            # GOGS uses /org/{orgname}/repos format (singular 'org')
            endpoint = f"/org/{self.gogs_org}/repos"
            logger.info(
                f"Creating repo in organization '{self.gogs_org}' using endpoint: "
                f"{endpoint}"
            )
        else:
            endpoint = "/user/repos"
            logger.info(
                f"Creating repo for user '{self.gogs_username}' using endpoint: "
                f"{endpoint}"
            )

        logger.debug(f"Repository data: {repo_data}")

        try:
            response = self._gogs_api_request("POST", endpoint, repo_data)
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 409:
                logger.info(f"Repository {project.name} already exists in GOGS")
                return {"name": project.name}  # Return minimal info

            if e.response.status_code == 404 and self.gogs_org:
                logger.exception(
                    f"Organization '{self.gogs_org}' not found or you don't have "
                    "permission to create repos in it. Please verify: 1) Organization "
                    "exists in GOGS, 2) Your token has org repo creation permissions"
                )
                raise

            logger.exception("GOGS API request failed")
            raise

    def _get_gogs_clone_url(self, repo_name: str) -> str:
        """Get the GOGS repository URL; authentication is supplied separately."""
        owner = self.gogs_org or self.gogs_username
        return (
            f"{self.gogs_url}/{quote(owner, safe='')}/{quote(repo_name, safe='')}.git"
        )

    def _push_repository_with_retry(
        self, origin: Remote, repo_name: str, retries: int = MAX_RETRIES
    ) -> None:
        """Push all refs, retrying transient failures but not rejected credentials."""
        with (
            self._git_credentials(self.gogs_username, self.gogs_token) as env,
            origin.repo.git.custom_environment(**env),
        ):
            _retry_operation(
                lambda: origin.push(mirror=True).raise_if_error(),
                f"Git push of {repo_name}",
                self._is_retryable_git_error,
                retries,
            )

    def _backup_repository(self, project: Any):
        """Backup a single repository from GitLab to GOGS."""
        # Create temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            repo: Repo | None = None
            try:
                # Clone from GitLab
                logger.info(f"Cloning {project.name} from GitLab...")
                repo = self._clone_repository_with_retry(
                    project,
                    temp_dir,
                )

                # Check if repo exists in GOGS, create if not
                if not self._check_gogs_repo_exists(project.name):
                    logger.info(f"Creating repository {project.name} in GOGS...")
                    self._create_gogs_repo(project)

                # Update remote to GOGS
                logger.info("Updating remote to GOGS...")
                if "origin" in [r.name for r in repo.remotes]:
                    repo.delete_remote(repo.remotes.origin)

                gogs_url = self._get_gogs_clone_url(project.name)
                origin = repo.create_remote("origin", gogs_url)

                # Push to GOGS (mirror push to sync all refs)
                logger.info(f"Pushing {project.name} to GOGS...")
                self._push_repository_with_retry(origin, project.name)

                logger.info(f"Successfully backed up {project.name}")

            finally:
                if repo is not None:
                    repo.close()

    def run(self):
        """Run the backup process for all repositories in the GitLab group."""
        try:
            # Get GitLab group
            group = self.gl.groups.get(self.gitlab_group_id)
            logger.info(f"Found GitLab group: {group.name}")

            # Get all projects in the group (including subgroups)
            projects = group.projects.list(
                all=True,
                include_subgroups=True,
                archived=False,  # Skip archived projects
            )

            logger.info(f"Found {len(projects)} projects to backup")

            # Track results
            successful = []
            failed = []

            # Backup each repository
            for idx, project in enumerate(projects):
                logger.info("")  # Visual Spacing
                project_display_name = (
                    getattr(project, "path_with_namespace", None)
                    or getattr(project, "name", None)
                    or str(project.id)
                )

                # Get full project details with retries for transient failures
                full_project = self._get_full_project_with_retry(
                    project.id,
                    project_display_name,
                )
                if full_project is None:
                    failed.append(
                        (
                            project_display_name,
                            "Failed to load project details after retries",
                        )
                    )
                    continue

                logger.info(
                    f"[{idx + 1:03d}/{len(projects):03d}] Starting backup of "
                    f"{full_project.path_with_namespace}"
                )

                try:
                    self._backup_repository(full_project)
                    successful.append(full_project.name)
                except Exception as e:
                    logger.exception(f"Failed to backup {full_project.name}")
                    failed.append((full_project.name, str(e)))

            # Summary
            logger.info("\nBackup Summary:")
            logger.info(f"Successful: {len(successful)} repositories")
            logger.info(f"Failed: {len(failed)} repositories")

            if failed:
                logger.error("\nFailed repositories:")
                for repo_name, error in failed:
                    logger.error(f"  - {repo_name}: {error}")

                # Exit with error code if any backups failed
                sys.exit(1)

        except gitlab.GitlabError:
            logger.exception("GitLab API error")
            sys.exit(1)

        except Exception:
            logger.exception("Unexpected error")
            sys.exit(1)


def main():
    """Main entry point."""
    logger.info("Starting GitLab to GOGS backup process...")

    try:
        backup = GitLabToGOGSBackup()
        backup.run()
        logger.info("Backup process completed successfully!")

    except Exception:
        logger.exception("Backup process failed")
        sys.exit(1)


if __name__ == "__main__":
    load_dotenv()
    main()
