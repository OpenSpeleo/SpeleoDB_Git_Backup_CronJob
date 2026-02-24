# Features

## Core backup behavior

- Connects to GitLab using token authentication.
- Reads all non-archived projects in a target GitLab group.
- Includes subgroup projects in discovery.
- Clones each repository with `--mirror` semantics.
- Creates destination repositories in GOGS when missing.
- Pushes all refs to GOGS with mirror push.

## Reliability features

- Retries transient GitLab project-detail API failures with exponential backoff.
- Retries transient git clone failures with exponential backoff.
- Skips failed projects and continues processing remaining repositories.
- Produces a final run summary with successful and failed repositories.

## Operational behavior

- Validates required environment variables at startup.
- Verifies target GOGS organization access when organization mode is enabled.
- Exits with status code `1` when one or more repositories fail.
