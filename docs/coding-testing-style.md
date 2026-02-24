# Coding, Testing, and Style Guide

## Coding conventions

- Use Python 3.14-compatible syntax and standard library APIs.
- Prefer type hints for new helper functions and non-trivial parameters.
- Keep functions focused on one responsibility.
- Do not log or print secrets (tokens, passwords, auth URLs).
- Prefer explicit exception handling over broad silent catches.

## Reliability and error-handling rules

- External calls (GitLab API, GOGS API, git operations) should be treated as
  failure-prone.
- Retry only transient failures.
- Use exponential backoff for retries.
- Log each retry attempt with:
  - operation context
  - current attempt and max attempt count
  - delay before next attempt
- When retries are exhausted for one repository, skip that repository and
  continue processing others.

## Logging standards

- `INFO`: high-level progress and lifecycle milestones.
- `WARNING`: transient/retryable conditions.
- `ERROR` or `logger.exception(...)`: terminal failures.
- Summary logging at the end must include total success/failure counts and the
  failed repository list.

## Testing and verification

Minimum checks for code changes:

```bash
uv run ruff check .
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile main.py
```

Recommended behavior tests:

- Run against a small GitLab test group with known repositories.
- Confirm repo creation path in GOGS (org and/or user mode).
- Simulate transient network failures and verify retry logs and continuation.

## Formatting and style

- Follow `pyproject.toml` Ruff configuration.
- Keep lines near or under 88 characters.
- Use clear naming, deterministic control flow, and concise comments.
