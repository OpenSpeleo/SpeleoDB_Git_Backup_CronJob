import { defineRailway, github, preserve, project, service } from "railway/iac";

// This repository owns only the backup service in the shared Railway project.
// Keep this name stable after the first apply.
export const partial = "speleodb-git-backup-cronjob";

export default defineRailway(() => {
  const backup = service("SpeleoDB Git Backup CronJob", {
    source: github("OpenSpeleo/SpeleoDB_Git_Backup_CronJob", {
      branch: "main",
      checkSuites: true,
    }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "Dockerfile",
    },
    replicas: 1,
    deploy: {
      runtime: "V2",
      cronSchedule: "0/15 * * * *",
      sleepApplication: false,
      restartPolicyType: "NEVER",
      // Preserve the existing production resource limits.
      limitOverride: {
        containers: {
          cpu: 1,
          memoryBytes: 2_000_000_000,
        },
      },
    },
    // IaC treats omitted variables as deletions; retain their Railway values.
    env: {
      FORCE_REBUILD_COUNTER: preserve(),
      GITLAB_GROUP_ID: preserve(),
      GITLAB_GROUP_NAME: preserve(),
      GITLAB_HOST_URL: preserve(),
      GITLAB_TOKEN: preserve(),
      GOGS_ACCESS_TOKEN: preserve(),
      GOGS_INSTANCE_URL: preserve(),
      GOGS_ORG: preserve(),
      GOGS_USERNAME: preserve(),
    },
  });
  return project("speleoDB", {
    resources: [backup],
  });
});
