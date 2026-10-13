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
- Pushes all refs to GOGS with mirror push.
- Authenticates Git operations over HTTP and HTTPS without credentials in URLs.

## Reliability features

- Retries transient failures on every GitLab API request, including
  authentication, group lookup, project listing, pagination, and project
  details.
- Retries transient GOGS API failures, including timeouts during repository
  checks, creation, and organization verification.
- Retries transient git clone and fetch failures with exponential backoff.
- Retains existing mirrors after failures and never pushes after a failed fetch.
- Publishes initial clones only after completion, keeping partial clones out of
  the persistent repository path.
- Locks each local mirror through fetch and push to prevent overlapping runs
  from changing its refs concurrently.
- Retries transient mirror push failures with exponential backoff.
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
- Refuses to start when `GIT_PROJECT_DIR` is unset or empty; tests use temporary
  directories without requiring this variable in the shell environment.
- Verifies target GOGS organization access when organization mode is enabled.
- Exits with status code `1` when one or more repositories fail.
