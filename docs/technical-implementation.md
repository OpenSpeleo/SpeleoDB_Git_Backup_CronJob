# Technical Implementation

## High-level architecture

The backup process is implemented in a single orchestrator class:
`GitLabToGOGSBackup` in `main.py`.

Responsibilities are split into helper methods:

- Configuration and validation:
  - `_validate_config()`
  - `_validate_http_url()`
  - `_git_credentials()`
  - `_verify_gogs_org_exists()`
- GOGS API helpers:
  - `_gogs_api_request()`
  - `_check_gogs_repo_exists()`
  - `_create_gogs_repo()`
  - `_get_gogs_clone_url()`
- Reliability helpers (module-level policy and orchestrator methods):
  - `_retry_operation()`
  - `_is_retryable_gitlab_error()`
  - `RetryingGitlab.http_request()`
  - `_get_full_project_with_retry()`
  - `_is_retryable_git_error()`
  - `_clone_repository_with_retry()`
  - `_push_repository_with_retry()`
- Backup orchestration:
  - `_backup_repository()`
  - `run()`

## Runtime flow

1. Initialize and validate environment configuration.
2. Authenticate against GitLab, with retry for transient API errors.
3. Resolve GitLab group and list eligible projects, retrying each API request
   independently, including pagination requests.
4. For each project:
   - Fetch full project details (with retry for transient API errors).
   - Clone repository from GitLab (with retry for transient clone errors).
   - Ensure destination repo exists in GOGS.
   - Push mirror refs to GOGS (with retry for transient transport errors).
   - Check the push result for rejected refs before reporting success.
   - Record success or failure.
5. Emit summary and exit non-zero when failures are present.

## Retry model

All GitLab API requests and Git clone/push operations share
`_retry_operation()`:

- Base delay: 1 second.
- Delays: `1, 2, 4, 8, 16` seconds.
- Hard limit: `MAX_RETRIES=5`, meaning:
  - 1 initial attempt
  - up to 5 retries
  - up to 6 total attempts
- No delay after the final failure; successful operations return immediately.
- Authentication, permission, invalid-request, and repository rejection errors
  are not retried.
- The backup always creates a fresh mirror clone; it has no `git pull` step.

### GitLab API retry

`RetryingGitlab.http_request()` covers authentication, group lookup, project
listing, every pagination request, and project detail lookup. It disables the
SDK's own retries to prevent nested retry loops from exceeding the limit. A
failed pagination request retries that page without replaying earlier pages.
Requests have a 30-second timeout.

Retryable failures include connection errors, timeouts, interrupted responses,
and HTTP 408, 429, 500, 502, 503, 504, and 520–524. HTTP authentication and
permission errors, malformed requests, and TLS certificate failures are
terminal.

`_get_full_project_with_retry()` handles a final request failure by skipping
that project; it does not add another retry loop. Failures during authentication
or group discovery end the run with a nonzero exit status.

### Clone retry

`_clone_repository_with_retry()` retries transient `GitCommandError` failures
(e.g. HTTP 5xx/RPC/connectivity signatures). Before each attempt, it cleans the
target clone path to avoid partial clone state issues.

### Push retry

`_push_repository_with_retry()` checks `PushInfoList.raise_if_error()` so a
rejected ref cannot count as a successful backup. Transient HTTP and connection
failures are retried; authentication and permission failures are terminal.

## Git authentication

Both clone and push use a temporary `GIT_ASKPASS` shell helper with credentials
supplied through the subprocess environment. The helper file contains no secrets
and is removed after the operation. Terminal prompts and credential caching are
disabled. Clone credentials are removed from the returned GitPython `Repo`
environment before it is reused for the GOGS push. Repository URLs contain no
credentials, including on failure paths. HTTP and HTTPS use the same mechanism.
See [Git credential handling](https://git-scm.com/docs/gitcredentials) and
[GitPython push results](https://gitpython.readthedocs.io/en/stable/reference.html#git.remote.PushInfoList.raise_if_error).

`GITLAB_HOST_URL` accepts a hostname (defaults to HTTPS) or a full HTTP(S) URL.
`GOGS_INSTANCE_URL` requires a full HTTP(S) URL. Base paths and ports are
preserved; embedded credentials, query parameters, and fragments are rejected
before use.

## Error handling strategy

- Retryable/transient failures are logged as `WARNING` with attempt information.
- Final unrecoverable failures are logged with stack traces.
- A failure for one project does not terminate processing of other projects.
