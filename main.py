# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "python-dotenv",
#     "python-gitlab==7.0.0",
#     "GitPython==3.1.45",
#     "requests==2.32.5"
# ]
# ///
"""
GitLab to GOGS Backup Script

This script backs up all repositories from a GitLab organization to GOGS.
It reads credentials from environment variables and uses GitPython for git operations.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from typing import Any

import gitlab
import requests
from dotenv import load_dotenv
from git import GitCommandError
from git import Repo

# Configure logging
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class GitLabToGOGSBackup:
    """Handles backing up GitLab repositories to GOGS."""

    def __init__(self):
        """Initialize with credentials from environment variables."""
        # GitLab configuration
        self.gitlab_url = f"https://{os.environ.get('GITLAB_HOST_URL', 'gitlab.com')}"
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
        self.gl = gitlab.Gitlab(self.gitlab_url, private_token=self.gitlab_token)
        self.gl.auth()

        # GOGS API headers
        self.gogs_headers = {
            "Authorization": f"token {self.gogs_token}",
            "Content-Type": "application/json",
        }

        # Verify GOGS organization exists if specified
        if self.gogs_org:
            self._verify_gogs_org_exists()

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

        # Ensure GOGS URL doesn't end with slash
        self.gogs_url = self.gogs_url.rstrip("/")

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
        """Get the GOGS repository clone URL with authentication."""
        if self.gogs_org:
            repo_path = f"{self.gogs_org}/{repo_name}"
        else:
            repo_path = f"{self.gogs_username}/{repo_name}"

        # Use HTTPS with token authentication
        gogs_base = self.gogs_url.replace(
            "https://", f"https://{self.gogs_username}:{self.gogs_token}@"
        )
        return f"{gogs_base}/{repo_path}.git"

    def _backup_repository(self, project: Any):
        """Backup a single repository from GitLab to GOGS."""
        # Create temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                # Clone from GitLab
                logger.info(f"Cloning {project.name} from GitLab...")
                gitlab_url = project.http_url_to_repo.replace(
                    "https://", f"https://oauth2:{self.gitlab_token}@"
                )

                repo = Repo.clone_from(
                    gitlab_url,
                    temp_dir,
                    mirror=True,  # Clone as mirror to get all refs
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
                origin.push(mirror=True)

                logger.info(f"Successfully backed up {project.name}")

            except GitCommandError:
                logger.exception(f"Git error while backing up {project.name}")
                raise

            except Exception:
                logger.exception(f"Failed to backup {project.name}")
                raise

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
                # Get full project details
                full_project = self.gl.projects.get(project.id)

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
