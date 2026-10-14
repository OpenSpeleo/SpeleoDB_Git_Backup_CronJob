# SpeleoDB_Git_Backup_CronJob

Mirrors non-archived GitLab group repositories (including subgroups) to GOGS.

## Configuration

Set these environment variables on the backup service:

| Variable                  | Value / purpose                                                          |
| ------------------------- | ------------------------------------------------------------------------ |
| `GITLAB_HOST_URL`         | `gitlab.com` (default), or a full HTTP(S) URL                            |
| `GITLAB_TOKEN`            | GitLab token with API and repository read access                         |
| `GITLAB_GROUP_ID`         | Source GitLab group ID                                                   |
| `GIT_PROJECT_DIR`         | Required writable directory for persistent local Git mirrors; no default |
| `BACKUP_PARALLEL_THREADS` | Maximum concurrent repository backups; integer >= 1, default `1`         |
| `GOGS_INSTANCE_URL`       | Full base URL, e.g. `http://gogs.railway.internal:3000`                  |
| `GOGS_USERNAME`           | GOGS account used for Git authentication                                 |
| `GOGS_ACCESS_TOKEN`       | GOGS token with access to create and push destination repos              |
| `GOGS_ORG`                | Optional destination organization; empty uses personal repos             |

Git authenticates non-interactively over HTTP or HTTPS using `GIT_ASKPASS`.
Tokens are passed through the Git subprocess environment, not repository URLs or
credential files. The helper requires `/bin/sh`, available in the Docker image.
Use HTTPS outside a trusted private network. Railway's internal hostname is
intended for services within the same project environment.

Run locally with `uv run main.py` after configuring a test group and
destination. Mirror pushes replace destination refs and delete refs absent from
the source.

`GIT_PROJECT_DIR` must be set to a nonempty path or startup fails before any
network calls. For example, set `GIT_PROJECT_DIR=/data/git-mirrors` on a
persistent volume. The directory is created as needed. Relative paths resolve
from the process working directory; absolute paths are recommended for cron
jobs. Container deployments must mount persistent storage at this path to retain
mirrors between runs and deployments.

Each mirror is stored under
`<GIT_PROJECT_DIR>/<instance-hash>/<project-id>.git`. The instance hash
separates GitLab servers, and project IDs keep local mirrors stable across
project renames. The first run clones; subsequent runs fetch only missing
objects. Forced fetches replace rewritten branches and tags and prune deleted
refs before pushing to GOGS, so GitLab always wins. No merge or rebase is
performed. Local mirrors for removed or archived projects are retained but are
no longer updated.

Set `BACKUP_PARALLEL_THREADS=4`, for example, to back up up to four repositories
concurrently. Unset means `1`; empty, non-integer, zero, and negative values
fail at startup before network calls. Higher counts increase CPU, memory, disk,
and network usage.

The main thread authenticates and lists all repositories, including subgroups,
with up to 100 repositories per page. The listing already contains the fields
needed for backup: no per-project GitLab detail requests are made. It
deduplicates project IDs and copies listing fields into immutable worker
records, then starts the pool immediately.

Each worker clones or fetches its mirror and ensures its GOGS destination
exists. New local mirrors and newly created destinations are always pushed. For
existing mirrors and destinations, the worker compares all local refs with GOGS
using `git ls-remote --refs` and skips the push when they match. This detects
changed, added, and deleted branches/tags, including destination-only changes.
Comparing against GOGS also ensures a previously failed push is attempted again
even when the next fetch changes nothing.

Repository names are assumed unique (UUIDs in the configured source
organization). Each worker owns its repository handles, temporary files,
credentials, and HTTP request state; shared configuration is only read. The main
thread collects results and reports failures after every backup finishes.
Progress logs include listing, worker startup, clone/fetch, pushes, and
unchanged repositories. Failure summaries retain discovery order.

## Troubleshooting

`could not read Username ... No such device or address` with an HTTP GOGS URL
was caused by the old script injecting credentials only into HTTPS URLs. Deploy
the updated script; the internal GOGS URL can keep its `http://` scheme.

An authentication rejection with the updated script requires checking the GOGS
username, access token, and repository permissions. Authentication failures are
not retried. All GitLab API calls (including authentication and pagination),
clones, fetches, ref comparisons, and pushes retry transient failures at most
five times after the initial attempt, waiting 2, 4, 8, 16, and 30 seconds. A
failed fetch skips the destination push and retains the local mirror for the
next run. An overlapping backup of the same local mirror fails that project
instead of racing its refs.

## Railway configuration

[`.railway/railway.ts`](.railway/railway.ts) manages the backup service using
Railway Infrastructure as Code, including its Dockerfile build and 15-minute
cron schedule. Existing variable values stay in Railway through `preserve()`.

Install the configuration SDK with `npm ci --prefix .railway`, link this
directory to the production backup service, and run `railway config plan`. Apply
reviewed changes with `railway config apply`; deploying application code alone
does not apply IaC changes. See
[the Railway configuration guide](.railway/README.md) for the complete workflow
and official documentation.

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
also verifies persistent reuse, upstream history rewrites, moved/deleted tags,
and failed-fetch handling. It does not contact production services or require
production credentials or `GIT_PROJECT_DIR` in the shell environment; tests
supply their own temporary directories.

Concurrency tests use bounded barriers and events to verify worker limits,
listing preparation, duplicate handling, and failure/retry isolation. The HTTP
fixture also checks parallel initial and incremental backups of multiple
repositories.

See [the documentation index](docs/README.md) for implementation details.
