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
  - `_is_retryable_http_error()`
  - `_is_retryable_gitlab_error()`
  - `RetryingGitlab.http_request()`
  - `_get_full_project_with_retry()`
  - `_is_retryable_git_error()`
  - `_clone_repository_with_retry()`
  - `_fetch_repository_with_retry()`
  - `_push_repository_with_retry()`
- Backup orchestration:
  - `_repository_path()`
  - `_repository_lock()`
  - `_clone_or_fetch_repository()`
  - `_backup_repository()`
  - `_prepare_projects()`
  - `_backup_project()`
  - `_backup_projects()`
  - `run()`

## Runtime flow

1. Initialize and validate environment configuration.
2. Authenticate against GitLab, with retry for transient API errors.
3. Resolve GitLab group and list eligible projects, retrying each API request
   independently, including pagination requests.
4. On the main thread, deduplicate project IDs, fetch all full project details
   with retries, and copy backup inputs into frozen `BackupProject` records.
   Record metadata failures without submitting those projects.
5. Prepare the shared mirror parent directory and submit each prepared project
   to `ThreadPoolExecutor(max_workers=BACKUP_PARALLEL_THREADS)`. Each job:
   - Lock the project's persistent mirror for the entire fetch/push sequence.
   - Clone a missing mirror or fetch the existing mirror from GitLab (with retry
     for transient transport errors).
   - Ensure destination repo exists in GOGS.
   - Push mirror refs to GOGS (with retry for transient transport errors).
   - Check the push result for rejected refs before reporting success.
6. On the main thread, collect completed futures with `as_completed()`, record
   successes and failures, and wait for executor shutdown.
7. Emit a summary in discovery order and exit non-zero when failures are
   present.

## Parallelism and ownership

`BACKUP_PARALLEL_THREADS` defaults to `1` and must parse as an integer >= 1. An
explicitly empty or invalid value fails before network calls. The same executor
path handles sequential and parallel backups; no executor is created when
preparation produces no jobs.

`BackupProject` contains only the project ID, name, namespace path, description,
visibility, and repository URL. No GitLab SDK object or session is passed to a
worker. Shared configuration is read only during execution; GOGS headers are
stored in an immutable mapping and copied for each request. `requests.request()`
creates a separate session for each call. Each job creates and closes its own
GitPython repository/remotes, credential environment, and temporary directories.
Workers do not change the process environment or working directory.

Unique UUID project names in the configured source organization identify unique
GOGS destinations. Repeated project IDs are skipped before metadata retrieval,
so a local mirror is scheduled at most once. The existing nonblocking per-mirror
file lock also protects against overlapping cron invocations using that mirror.

Only the main thread submits futures, waits for their results, and updates
result collections. Workers never wait on another job or submit nested work. A
retry sleeps only its own worker. Logging uses Python's thread-safe logging
machinery; progress messages may interleave, while failure summaries use
discovery order. No additional application locks are required between workers.
Higher parallelism increases resource usage and simultaneous load on both Git
servers.

## Retry model

All GitLab and GOGS API requests and Git clone/fetch/push operations share
`_retry_operation()`:

- Base delay: 2 seconds; maximum delay: 30 seconds.
- Delays: `2, 4, 8, 16, 30` seconds (60 seconds of waiting in total, plus the
  time spent making requests).
- Hard limit: `MAX_RETRIES=5`, meaning:
  - 1 initial attempt
  - up to 5 retries
  - up to 6 total attempts
- No delay after the final failure; successful operations return immediately.
- Authentication, permission, invalid-request, and repository rejection errors
  are not retried.
- Existing mirrors use forced, pruning fetches; there is no `git pull` step.

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
or group discovery end the run with a nonzero exit status. Expected API failures
are logged with the exception type and HTTP status when available, without
response bodies or tracebacks. Retry warnings also include this safe error
summary, so a repeated 503 is visible before retries run out.

### GOGS API retry

`_gogs_api_request()` applies the shared HTTP retry policy to organization
verification, repository existence checks, and repository creation. Each attempt
has a 30-second timeout. Missing repositories (404) and creation conflicts (409)
are handled by their callers without retrying those responses; a conflict after
a timed-out creation is treated as an already-existing repository.

An exhausted or permanent GOGS request failure during backup is logged as a
concise error without a traceback. The repository is included in the failure
summary, its persistent mirror is retained, and the next project is processed.
The run still exits nonzero if any repository failed.

### Clone retry

`_clone_repository_with_retry()` retries transient `GitCommandError` failures
(e.g. HTTP 5xx/RPC/connectivity signatures). Before each attempt, it cleans the
target clone path to avoid partial clone state issues.

### Persistent mirrors and fetch retry

`GIT_PROJECT_DIR` is required with no default. An unset or whitespace-only value
fails before authentication. Mirrors live at
`<GIT_PROJECT_DIR>/<sha256(gitlab_url)[:16]>/<project-id>.git`, keeping
identities stable across project renames while separating GitLab instances. Use
persistent storage for this directory in container deployments.

Initial clones use a temporary staging directory on the same filesystem and are
renamed into place only after success. Failed initial clones are cleaned up;
existing mirrors are never deleted on fetch or push failures. Invalid or nonbare
repositories fail the project without being overwritten.

`origin` remains the GitLab remote; its URL is refreshed on each fetch to follow
project moves. Existing mirrors run the equivalent of:

```bash
git fetch --atomic --force --prune origin '+refs/*:refs/*'
```

The forced refspec overwrites rewritten branches and tags, and pruning covers
all refs in that mapping, including deleted tags. Atomic fetch updates all refs
or none. See the [Git fetch documentation](https://git-scm.com/docs/git-fetch).
Transient fetch failures use the shared retry policy; a terminal fetch failure
prevents the GOGS push, so stale local refs cannot be reported as a fresh
backup.

GOGS uses a separate `gogs` remote with no fetch refspec, avoiding destination
tracking refs inside the mirror. A per-project advisory `flock` (Linux/macOS)
spans clone/fetch and push. Lock contention fails that project and processing
continues; lock files remain, while OS locks release when the process exits.
Mirrors for archived or removed projects remain on disk without further updates.

### Push retry

`_push_repository_with_retry()` checks `PushInfoList.raise_if_error()` so a
rejected ref cannot count as a successful backup. Transient HTTP and connection
failures are retried; authentication and permission failures are terminal.

## Git authentication

Clone, fetch, and push use a temporary `GIT_ASKPASS` shell helper with
credentials supplied through the subprocess environment. The helper file
contains no secrets and is removed after the operation. Terminal prompts and
credential caching are disabled. Clone credentials are removed from the returned
GitPython `Repo` environment before it is reused for the GOGS push. Repository
URLs contain no credentials, including on failure paths. HTTP and HTTPS use the
same mechanism. See
[Git credential handling](https://git-scm.com/docs/gitcredentials) and
[GitPython push results](https://gitpython.readthedocs.io/en/stable/reference.html#git.remote.PushInfoList.raise_if_error).

`GITLAB_HOST_URL` accepts a hostname (defaults to HTTPS) or a full HTTP(S) URL.
`GOGS_INSTANCE_URL` requires a full HTTP(S) URL. Base paths and ports are
preserved; embedded credentials, query parameters, and fragments are rejected
before use.

## Error handling strategy

- Retryable/transient failures are logged as `WARNING` with attempt information.
- Terminal GOGS request failures during backup are logged without stack traces;
  unexpected exceptions retain tracebacks for debugging.
- A failure for one project does not terminate processing of other projects.
