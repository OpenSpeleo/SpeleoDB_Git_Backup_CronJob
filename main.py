"""
GitLab to GOGS Backup Script

This script backs up all repositories from a GitLab organization to GOGS.
It reads credentials from environment variables and uses GitPython for git operations.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
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
RETRY_BASE_DELAY_SECONDS = 2
RETRY_MAX_DELAY_SECONDS = 30
TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


@dataclass(frozen=True, slots=True)
class BackupProject:
    """Immutable backup inputs, detached from the main thread's GitLab client."""

    id: int
    name: str
    path_with_namespace: str
    description: str
    visibility: str
    http_url_to_repo: str


def _request_error_summary(error: Exception) -> str:
    """Describe failures without logging response bodies or credentialed URLs."""
    status = None
    if isinstance(error, gitlab.GitlabError):
        status = error.response_code
    elif isinstance(error, requests.exceptions.RequestException):
        if error.response is not None:
            status = error.response.status_code
    summary = type(error).__name__
    if status is not None:
        summary += f" (HTTP {status})"
    return summary


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
            delay_seconds = min(
                RETRY_BASE_DELAY_SECONDS * 2**attempt, RETRY_MAX_DELAY_SECONDS
            )
            logger.warning(
                "%s failed transiently: %s (attempt %s/%s). Retrying in %s seconds...",
                description,
                _request_error_summary(error),
                attempt + 1,
                retries + 1,
                delay_seconds,
            )
            time.sleep(delay_seconds)
    raise RuntimeError("Unexpected retry state")


def _is_retryable_http_error(error: Exception) -> bool:
    """Retry temporary HTTP/transport failures, never auth or invalid requests."""
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


def _is_retryable_gitlab_error(error: Exception) -> bool:
    """Apply the shared HTTP policy to GitLab SDK errors too."""
    if isinstance(error, gitlab.GitlabError):
        return error.response_code in TRANSIENT_HTTP_STATUSES or isinstance(
            error, gitlab.GitlabConnectionError
        )
    return _is_retryable_http_error(error)


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
        backup_dir = os.environ.get("GIT_PROJECT_DIR", "").strip()
        if not backup_dir:
            raise ValueError("GIT_PROJECT_DIR is required and must not be empty")
        self.backup_dir = Path(backup_dir).expanduser().resolve()
        try:
            self.parallel_threads = int(os.environ.get("BACKUP_PARALLEL_THREADS", "1"))
        except ValueError:
            raise ValueError(
                "BACKUP_PARALLEL_THREADS must be an integer >= 1"
            ) from None
        if self.parallel_threads < 1:
            raise ValueError("BACKUP_PARALLEL_THREADS must be an integer >= 1")

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
        logger.info("Git project directory: %s", self.backup_dir)
        logger.info("Backup parallel threads: %s", self.parallel_threads)
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
        self.gogs_headers = MappingProxyType(
            {
                "Authorization": f"token {self.gogs_token}",
                "Content-Type": "application/json",
            }
        )

        # Verify GOGS organization exists if specified
        if self.gogs_org:
            self._verify_gogs_org_exists()

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
        self, project: BackupProject, destination_dir: str, retries: int = MAX_RETRIES
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

    def _repository_path(self, project: BackupProject) -> Path:
        """Keep project renames stable and separate different GitLab instances."""
        instance = hashlib.sha256(self.gitlab_url.encode()).hexdigest()[:16]
        return self.backup_dir / instance / f"{int(project.id)}.git"

    @staticmethod
    @contextmanager
    def _repository_lock(repo_path: Path) -> Iterator[None]:
        """Protect the entire fetch/push sequence against overlapping cron runs."""
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        with repo_path.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "Repository is in use by another backup run"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _fetch_repository_with_retry(self, repo: Repo, project: BackupProject) -> None:
        """Replace all local refs with upstream refs, including rewritten tags."""
        gitlab_url = self._validate_http_url(
            project.http_url_to_repo, "GitLab repository URL"
        )
        repo.remotes.origin.set_url(gitlab_url)
        with (
            self._git_credentials("oauth2", self.gitlab_token) as env,
            repo.git.custom_environment(**env),
        ):
            _retry_operation(
                lambda: repo.git.fetch(
                    "origin", "+refs/*:refs/*", force=True, prune=True, atomic=True
                ),
                f"Git fetch of {project.name}",
                self._is_retryable_git_error,
            )
        logger.info("Successfully fetched %s from GitLab", project.name)

    def _clone_or_fetch_repository(
        self, project: BackupProject, repo_path: Path
    ) -> Repo:
        """Publish only complete clones; preserve existing mirrors on failure."""
        if not repo_path.exists():
            logger.info("Cloning %s from GitLab...", project.name)
            with tempfile.TemporaryDirectory(
                prefix=f"{repo_path.stem}-clone-", dir=repo_path.parent
            ) as staging_dir:
                repo = self._clone_repository_with_retry(project, staging_dir)
                repo.close()
                (Path(staging_dir) / "mirror-repo.git").rename(repo_path)
            logger.info("Successfully cloned %s from GitLab", project.name)
            return Repo(repo_path)

        logger.info("Fetching %s from GitLab...", project.name)
        repo = Repo(repo_path)
        if not repo.bare:
            repo.close()
            raise ValueError("Persistent backup repository must be bare")
        try:
            self._fetch_repository_with_retry(repo, project)
        except Exception:
            repo.close()
            raise
        return repo

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
        """Make a GOGS API request with bounded retries for transient failures."""
        url = f"{self.gogs_url}/api/v1{endpoint}"

        def request() -> requests.Response:
            response = requests.request(
                method=method,
                url=url,
                headers=dict(self.gogs_headers),
                json=data,
                timeout=30,
            )
            response.raise_for_status()
            return response

        return _retry_operation(
            request, f"GOGS {method.upper()} {endpoint}", _is_retryable_http_error
        )

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
            raise

    def _create_gogs_repo(self, project: BackupProject) -> dict[str, Any]:
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
                logger.error(  # noqa: TRY400 - expected API failure, no traceback
                    f"Organization '{self.gogs_org}' not found or you don't have "
                    "permission to create repos in it. Please verify: 1) Organization "
                    "exists in GOGS, 2) Your token has org repo creation permissions"
                )
                raise

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

    def _destination_is_current(self, repo: Repo, project: BackupProject) -> bool:
        """Compare all refs with GOGS, including tags and deleted branches."""
        local_refs = set(
            repo.git.for_each_ref("--format=%(objectname)%09%(refname)").splitlines()
        )
        with (
            self._git_credentials(self.gogs_username, self.gogs_token) as env,
            repo.git.custom_environment(**env),
        ):
            remote_refs = _retry_operation(
                lambda: repo.git.ls_remote(
                    "--refs", self._get_gogs_clone_url(project.name)
                ),
                f"Git ref comparison for {project.name}",
                self._is_retryable_git_error,
            )
        return local_refs == set(remote_refs.splitlines())

    def _backup_repository(self, project: BackupProject) -> None:
        """Backup a single repository from GitLab to GOGS."""
        repo_path = self._repository_path(project)
        with self._repository_lock(repo_path):
            repo: Repo | None = None
            try:
                is_new = not repo_path.exists()
                repo = self._clone_or_fetch_repository(project, repo_path)

                # Check if repo exists in GOGS, create if not
                destination_exists = self._check_gogs_repo_exists(project.name)
                if not destination_exists:
                    logger.info(f"Creating repository {project.name} in GOGS...")
                    self._create_gogs_repo(project)

                if (
                    not is_new
                    and destination_exists
                    and self._destination_is_current(repo, project)
                ):
                    logger.info("No ref changes for %s; skipping push", project.name)
                    return

                # Keep origin pointing at GitLab for the next incremental fetch.
                gogs_url = self._get_gogs_clone_url(project.name)
                if "gogs" in repo.remotes:
                    destination = repo.remotes.gogs
                    destination.set_url(gogs_url)
                else:
                    destination = repo.create_remote("gogs", gogs_url)
                # GOGS must never create tracking refs inside the source mirror.
                with destination.config_writer as config:
                    config.remove_option("fetch")

                # Push to GOGS (mirror push to sync all refs)
                logger.info(f"Pushing {project.name} to GOGS...")
                self._push_repository_with_retry(destination, project.name)

                logger.info(f"Successfully backed up {project.name}")

            finally:
                if repo is not None:
                    repo.close()

    def _prepare_projects(
        self, projects: list[Any]
    ) -> tuple[dict[int, BackupProject], dict[int, tuple[str, str]]]:
        """Copy listing data into immutable worker inputs without network calls."""
        prepared: dict[int, BackupProject] = {}
        failed: dict[int, tuple[str, str]] = {}
        seen_ids: set[int] = set()
        for idx, project in enumerate(projects):
            project_id = int(project.id)
            if project_id in seen_ids:
                logger.info("Skipping repeated GitLab project id=%s", project_id)
                continue
            seen_ids.add(project_id)
            display_name = (
                getattr(project, "path_with_namespace", None)
                or getattr(project, "name", None)
                or str(project_id)
            )
            try:
                prepared[idx] = BackupProject(
                    id=project_id,
                    name=project.name,
                    path_with_namespace=project.path_with_namespace,
                    description=project.description or "",
                    visibility=project.visibility,
                    http_url_to_repo=self._validate_http_url(
                        project.http_url_to_repo, "GitLab repository URL"
                    ),
                )
            except Exception as error:
                logger.exception("Failed to prepare backup of %s", display_name)
                failed[idx] = (display_name, str(error))
        return prepared, failed

    def _backup_project(self, project: BackupProject, idx: int, total: int) -> None:
        """Run one independent job; results and GitLab access belong to the caller."""
        logger.info(
            "[%03d/%03d] Starting backup of %s",
            idx + 1,
            total,
            project.path_with_namespace,
        )
        self._backup_repository(project)

    def _backup_projects(
        self, projects: dict[int, BackupProject], total: int
    ) -> tuple[list[str], dict[int, tuple[str, str]]]:
        """Only the calling thread submits work and records completed futures."""
        successful: list[str] = []
        failed: dict[int, tuple[str, str]] = {}
        if not projects:
            return successful, failed

        first_project = next(iter(projects.values()))
        self._repository_path(first_project).parent.mkdir(parents=True, exist_ok=True)
        # Workers only read configuration. Repo handles, HTTP sessions, temporary
        # paths, and Git environments are created inside each individual job.
        logger.info(
            "Starting up to %s backup workers for %s repositories",
            self.parallel_threads,
            len(projects),
        )
        with ThreadPoolExecutor(
            max_workers=self.parallel_threads, thread_name_prefix="backup"
        ) as executor:
            futures = {
                executor.submit(self._backup_project, project, idx, total): idx
                for idx, project in projects.items()
            }
            for future in as_completed(futures):
                idx = futures[future]
                project = projects[idx]
                try:
                    future.result()
                    successful.append(project.name)
                except requests.exceptions.RequestException as error:
                    reason = f"GOGS API request failed: {_request_error_summary(error)}"
                    logger.error(  # noqa: TRY400 - expected request failure
                        "Failed to backup %s: %s. Skipping project.",
                        project.name,
                        reason,
                    )
                    failed[idx] = (project.name, reason)
                except Exception as error:
                    logger.exception("Failed to backup %s", project.name)
                    failed[idx] = (project.name, str(error))
        return successful, failed

    def run(self) -> None:
        """Prepare on the main thread, back up independently, then report results."""
        try:
            group = self.gl.groups.get(self.gitlab_group_id, with_projects=False)
            logger.info("Found GitLab group: %s", group.name)
            logger.info("Listing GitLab repositories (including subgroups)...")
            projects = group.projects.list(
                all=True,
                include_subgroups=True,
                archived=False,
                simple=False,
                per_page=100,
            )
            logger.info("Found %s projects to backup", len(projects))

            prepared, failed = self._prepare_projects(projects)
            successful, backup_failures = self._backup_projects(prepared, len(projects))
            failed.update(backup_failures)

            # Summary
            logger.info("\nBackup Summary:")
            logger.info(f"Successful: {len(successful)} repositories")
            logger.info(f"Failed: {len(failed)} repositories")

            if failed:
                logger.error("\nFailed repositories:")
                for idx in sorted(failed):
                    repo_name, error = failed[idx]
                    logger.error(f"  - {repo_name}: {error}")

                # Exit with error code if any backups failed
                sys.exit(1)

        except (gitlab.GitlabError, requests.exceptions.RequestException) as error:
            logger.error(  # noqa: TRY400 - expected API failure, no response body
                "GitLab API request failed: %s", _request_error_summary(error)
            )
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

    except (gitlab.GitlabError, requests.exceptions.RequestException) as error:
        logger.error(  # noqa: TRY400 - expected API failure, no response body
            "Backup process failed: %s", _request_error_summary(error)
        )
        sys.exit(1)

    except Exception:
        logger.exception("Backup process failed")
        sys.exit(1)


if __name__ == "__main__":
    load_dotenv()
    main()
