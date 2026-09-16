# Railway infrastructure configuration

`railway.ts` replaces the former root `railway.toml`. It manages the
`SpeleoDB Git Backup CronJob` service in the `speleoDB` project.

This repository is separate from the repositories for the project's other
services, so it uses the named partial `speleodb-git-backup-cronjob`. Keep that
name stable after applying it. The partial limits resource ownership to this
service; it does not manage the rest of the project.

## Preserved settings

| Setting              | Value                                  |
| -------------------- | -------------------------------------- |
| Builder              | Dockerfile                             |
| Dockerfile path      | `Dockerfile`                           |
| Runtime              | `V2`                                   |
| Replicas             | 1                                      |
| Cron schedule        | `0/15 * * * *` (every 15 minutes, UTC) |
| Application sleeping | Disabled                               |
| Restart policy       | `NEVER`                                |

The existing GitHub source, check-suite requirement, and production CPU/memory
limits are also declared to preserve them during reconciliation. All existing
service variables use `preserve()`; their values remain in Railway. When adding
a variable in Railway, add its name with `preserve()` here before the next
apply, because omitted variables are planned for deletion.

## Install and validate

Use Node.js 22 or newer and Railway CLI 5.49.1 or newer. From the repository
root:

```bash
npm ci --prefix .railway
railway link --project speleoDB --environment production \
  --service "SpeleoDB Git Backup CronJob"
railway config plan
```

Review the plan, then apply it:

```bash
railway config apply
railway config plan --detailed-exit-code
```

The second plan should report no changes. The migration command has cleared the
service's legacy Railway Config File setting; do not set that field to the
TypeScript file. IaC is evaluated by `railway config plan/apply`, rather than
being loaded automatically from the repository during application deployments.
Changes to this configuration therefore require a separate plan/apply step.

The SDK and lockfile live in `.railway/` and are excluded from the Python Docker
image. Application dependencies remain managed by `uv`.

Pre-commit formats `.railway/` TypeScript and JSON files with Prettier. The
existing Markdown formatter and whitespace, JSON, and secret checks also apply
to matching files here. Installed `node_modules/` dependencies are excluded.

## Official references

- [Infrastructure as Code and migration](https://docs.railway.com/infrastructure-as-code#migrating-from-config-as-code)
- [TypeScript IaC reference](https://docs.railway.com/infrastructure-as-code/reference)
- [Single-service partial ownership](https://docs.railway.com/infrastructure-as-code#one-file-per-project)
