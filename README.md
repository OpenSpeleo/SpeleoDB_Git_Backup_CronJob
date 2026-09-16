# SpeleoDB_Git_Backup_CronJob

Mirrors non-archived GitLab group repositories (including subgroups) to GOGS.

## Configuration

Set these environment variables on the backup service:

| Variable            | Value / purpose                                              |
| ------------------- | ------------------------------------------------------------ |
| `GITLAB_HOST_URL`   | `gitlab.com` (default), or a full HTTP(S) URL                |
| `GITLAB_TOKEN`      | GitLab token with API and repository read access             |
| `GITLAB_GROUP_ID`   | Source GitLab group ID                                       |
| `GOGS_INSTANCE_URL` | Full base URL, e.g. `http://gogs.railway.internal:3000`      |
| `GOGS_USERNAME`     | GOGS account used for Git authentication                     |
| `GOGS_ACCESS_TOKEN` | GOGS token with access to create and push destination repos  |
| `GOGS_ORG`          | Optional destination organization; empty uses personal repos |

Git authenticates non-interactively over HTTP or HTTPS using `GIT_ASKPASS`.
Tokens are passed through the Git subprocess environment, not repository URLs or
credential files. The helper requires `/bin/sh`, available in the Docker image.
Use HTTPS outside a trusted private network. Railway's internal hostname is
intended for services within the same project environment.

Run locally with `uv run main.py` after configuring a test group and
destination. Mirror pushes replace destination refs and delete refs absent from
the source.

## Troubleshooting

`could not read Username ... No such device or address` with an HTTP GOGS URL
was caused by the old script injecting credentials only into HTTPS URLs. Deploy
the updated script; the internal GOGS URL can keep its `http://` scheme.

An authentication rejection with the updated script requires checking the GOGS
username, access token, and repository permissions. Authentication failures are
not retried. All GitLab API calls (including authentication and pagination),
clones, and pushes retry transient failures at most five times after the initial
attempt, waiting 1, 2, 4, 8, and 16 seconds. The script creates fresh mirror
clones and does not perform `git pull`.

`railway.toml` currently leaves `cronSchedule` commented out. Configure a
schedule in Railway or enable that setting if periodic execution is intended.

## Validation

```bash
uv run ruff check .
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile main.py
uv run python -m unittest discover -v
```

The test suite uses a local test group fixture, temporary repositories, and an
authenticated loopback HTTP Git server. It exercises real clone and mirror push,
GOGS API creation through a test fixture, branch/tag replication and deletion,
authentication failure reporting, retries, and continuation after failures. It
does not contact production services or require production credentials.

See [the documentation index](docs/README.md) for implementation details.
