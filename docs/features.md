# Features

## Core backup behavior

- Connects to GitLab using token authentication.
- Reads all non-archived projects in a target GitLab group.
- Includes subgroup projects in discovery.
- Clones each repository with `--mirror` semantics.
- Creates destination repositories in GOGS when missing.
- Pushes all refs to GOGS with mirror push.
- Authenticates Git operations over HTTP and HTTPS without credentials in URLs.

## Reliability features

- Retries transient failures on every GitLab API request, including
  authentication, group lookup, project listing, pagination, and project
  details.
- Retries transient git clone failures with exponential backoff.
- Retries transient mirror push failures with exponential backoff.
- Uses at most five retries per operation, delayed by 1, 2, 4, 8, and 16
  seconds.
- Treats rejected pushes as failures and avoids retrying authentication errors.
- Skips failed projects and continues processing remaining repositories.
- Produces a final run summary with successful and failed repositories.

## Operational behavior

- Validates required environment variables at startup.
- Verifies target GOGS organization access when organization mode is enabled.
- Exits with status code `1` when one or more repositories fail.
