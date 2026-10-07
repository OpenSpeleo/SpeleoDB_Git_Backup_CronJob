# AGENTS.md

This file provides implementation and collaboration guidance for humans and AI
agents working in this repository.

## Project purpose

`SpeleoDB_Git_Backup_CronJob` mirrors all non-archived repositories from a
GitLab group (including subgroups) into a GOGS instance.

## Current feature set

- Authenticates to GitLab using a personal access token.
- Lists all eligible repositories from the configured GitLab group.
- Fetches full project details before backup.
- Clones each repository as a mirror.
- Creates missing destination repositories in GOGS.
- Pushes all refs/tags/branches to GOGS using mirror push.
- Tracks successes and failures and exits non-zero when failures exist.
- Retries transient GitLab project-detail API failures with exponential backoff.
- Retries transient Git clone failures with exponential backoff.

## Temporary agent files

Keep agent plans, task lists, TODO tracking, progress notes, review notes, and
scratch lessons outside the repository tree, including all submodules. Use a
unique task directory under `/tmp/` (for example, create one with
`mktemp -d /tmp/sdb-git-backup-cronjob-task.XXXXXX`) or another OS temporary
directory whose resolved path is outside every checkout.

Never create or update these working files inside the checkout, even in ignored
directories such as `tasks/`, `todos/`, `.cursor`, or `plans/`. Never stage or
commit them.

Existing tracked task and lesson files are historical references; do not append
new work to them. Keep durable product and architecture documentation in
`docs/`, without embedding task checklists or linking to temporary files. Before
an authorized commit, inspect the staged filenames and exclude all agent working
files.

## Technical implementation snapshot

- Main entrypoint: `main.py`.
- Main orchestration class: `GitLabToGOGSBackup`.
- Network + API responsibilities:
  - GitLab: group/project discovery and project detail retrieval.
  - GOGS: repository existence checks and repo creation.
- Git operations:
  - Clone from GitLab using `Repo.clone_from(..., mirror=True)`.
  - Push to GOGS via mirror remote push.
- Reliability model:
  - Retry transient external errors.
  - Skip only the failing project when possible.
  - Continue processing the remaining repositories.

## Coding rules

- Use Python 3.14-compatible code and maintain compatibility with existing
  project dependencies.
- Use explicit type hints for new helpers and non-trivial functions and methods.
- Keep functions focused and composable; prefer helper methods over deeply
  nested logic.
- Never log secrets (tokens/passwords); treat URLs with credentials as
  sensitive.
- Treat external I/O (GitLab API, GOGS API, git clone/push) as failure-prone:
  detect transient failures and retry them with exponential backoff.
- Log retry attempts with operation context, attempt number, maximum attempt
  count, and delay before the next attempt. Log the final success or failure.
- Preserve resilience: one failed project must not abort other project backups.
- Use Python's `logging` module for progress and failures; do not use `print`.
- Use structured logging levels:
  - `INFO`: normal progress.
  - `WARNING`: retryable/transient failures.
  - `ERROR/EXCEPTION`: terminal failures.
- Avoid swallowing exceptions silently.

## Testing and validation rules

After code edits and before merging changes, run:

```bash
uv run ruff check .
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile main.py
```

When behavior changes in backup flow, also run a safe end-to-end check against a
test group/repository set.

## Style rules

- Follow `pyproject.toml` (`ruff` + formatting expectations).
- Target max line length of 88 characters.
- Prefer clear, deterministic code over clever/compact constructs.
- Keep comments concise and useful; avoid redundant comments.

## Documentation map

- `docs/README.md` - documentation index
- `docs/features.md` - feature documentation
- `docs/technical-implementation.md` - architecture and runtime flow
- `docs/coding-testing-style.md` - coding, testing, and style conventions
