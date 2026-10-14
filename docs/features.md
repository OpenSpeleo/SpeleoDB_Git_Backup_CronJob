# Features

## Core backup behavior

- Connects to GitLab using token authentication.
- Reads all non-archived projects in a target GitLab group.
- Includes subgroup projects in discovery.
- Stores persistent bare mirrors in the required `GIT_PROJECT_DIR` directory.
- Clones missing repositories with `--mirror` semantics and fetches existing
  ones.
- Makes upstream authoritative: accepts force pushes and moved tags, and prunes
  deleted branches, tags, and other refs before mirroring to GOGS.
- Creates destination repositories in GOGS when missing.
- Mirror-pushes new repositories and changed refs; skips pushes when GOGS refs
  match the fetched mirror.
- Authenticates Git operations over HTTP and HTTPS without credentials in URLs.
- Backs up independent repositories with a configurable thread pool. Set
  `BACKUP_PARALLEL_THREADS` to an integer >= 1; the default is `1`.
- Lists repositories on the main thread with 100 results per page, uses listing
  fields directly without per-project detail requests, and passes immutable
  records to workers. Results are collected centrally.

## Reliability features

- Retries transient failures on every GitLab API request, including
  authentication, group lookup, project listing, and pagination.
- Retries transient GOGS API failures, including timeouts during repository
  checks, creation, and organization verification.
- Retries transient git clone and fetch failures with exponential backoff.
- Retains existing mirrors after failures and never pushes after a failed fetch.
- Publishes initial clones only after completion, keeping partial clones out of
  the persistent repository path.
- Locks each local mirror through fetch and push to prevent overlapping runs
  from changing its refs concurrently.
- Retries transient Git ref comparison and mirror push failures with exponential
  backoff. Comparing against GOGS ensures a previous failed push is attempted
  again even if the next fetch changes nothing.
- Uses at most five retries per operation, delayed by 2, 4, 8, 16, and 30
  seconds.
- Treats rejected pushes as failures and avoids retrying authentication errors.
- Skips failed projects and continues processing remaining repositories.
- Reports terminal GOGS request failures without tracebacks.
- Reports terminal GitLab API failures with the error type and HTTP status,
  without dumping response bodies or tracebacks.
- Produces a final run summary with successful and failed repositories.

## Operational behavior

- Validates required environment variables at startup.
- Rejects invalid `BACKUP_PARALLEL_THREADS` values before network calls.
- Deduplicates repeated source project IDs before scheduling backups;
  destination names remain the source's unique UUID names.
- Refuses to start when `GIT_PROJECT_DIR` is unset or empty; tests use temporary
  directories without requiring this variable in the shell environment.
- Verifies target GOGS organization access when organization mode is enabled.
- Exits with status code `1` when one or more repositories fail.
