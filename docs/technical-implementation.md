# Technical Implementation

## High-level architecture

The backup process is implemented in a single orchestrator class:
`GitLabToGOGSBackup` in `main.py`.

Responsibilities are split into helper methods:

- Configuration and validation:
  - `_validate_config()`
  - `_verify_gogs_org_exists()`
- GOGS API helpers:
  - `_gogs_api_request()`
  - `_check_gogs_repo_exists()`
  - `_create_gogs_repo()`
  - `_get_gogs_clone_url()`
- Reliability helpers:
  - `_is_retryable_gitlab_error()`
  - `_get_full_project_with_retry()`
  - `_is_retryable_clone_error()`
  - `_clone_repository_with_retry()`
- Backup orchestration:
  - `_backup_repository()`
  - `run()`

## Runtime flow

1. Initialize and validate environment configuration.
2. Authenticate against GitLab.
3. Resolve GitLab group and list eligible projects.
4. For each project:
   - Fetch full project details (with retry for transient API errors).
   - Clone repository from GitLab (with retry for transient clone errors).
   - Ensure destination repo exists in GOGS.
   - Push mirror refs to GOGS.
   - Record success or failure.
5. Emit summary and exit non-zero when failures are present.

## Retry model

Both retry helpers currently use exponential backoff:

- Base delay: 1 second.
- Growth: `1, 2, 4, 8, 16, ...` seconds.
- Configuration in current code path: `retries=5`, meaning:
  - 1 initial attempt
  - up to 5 retries
  - up to 6 total attempts

### Project detail fetch retry

`_get_full_project_with_retry()` retries transient GitLab/API errors and skips the
project after retries are exhausted.

### Clone retry

`_clone_repository_with_retry()` retries transient `GitCommandError` failures
(e.g. HTTP 5xx/RPC/connectivity signatures). Before each attempt, it cleans the
target clone path to avoid partial clone state issues.

## Error handling strategy

- Retryable/transient failures are logged as `WARNING` with attempt information.
- Final unrecoverable failures are logged with stack traces.
- A failure for one project does not terminate processing of other projects.
