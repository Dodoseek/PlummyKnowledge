# Upgrade

## What a release promises

From 1.0.0, PlummyKnowledge follows Semantic Versioning for its public contract: the REST API and SSE, audit
events, the CLI, environment variables and the configuration file, `compose.prod.yaml` with `deploy/scripts/`, and
the backup format ([ADR-025](../adr/0025-semver-and-contract-stability.md)):

- **patch** (`1.2.3` → `1.2.4`): fixes only;
- **minor** (`1.2` → `1.3`): new features; nothing you rely on is removed, only marked deprecated;
- **major** (`1.x` → `2.0`): may break the contract; the release notes describe every manual step.

Any 1.x release upgrades from any earlier 1.x without manual steps. Security fixes are published for the latest
minor release. Read the release notes in [CHANGELOG.md](../../CHANGELOG.md) before upgrading.

## Steps

1. **Back up** ([Backup and restore](backup-restore.md)). Downgrades are not supported: going back means
   restoring the backup made before the upgrade.
2. **Check out the new release** in the repository you installed from:

   ```bash
   git fetch --tags && git checkout vX.Y.Z
   ```

3. **Do the manual steps** from the release notes, if any (for example a new extension or a one-time script).
4. **Rebuild and restart:**

   ```bash
   docker compose -f compose.prod.yaml --env-file deploy/prod/.env --profile s3 up -d --build --wait
   ```

   `migrate` applies the database migrations and grants before the other services start. Keep the same `-f`
   override files and `--profile` you use normally.
5. **Check** readiness (`/api/v1/health/ready`), sign-in, search and an answer with a citation.

Images are built from the release tag on your host; they are not published to a registry.

## From versions before 0.5.0

0.5.0 split the database into a schema owner and a runtime role. Upgrading an installation created earlier needs
one manual step, once:

1. Back up.
2. `git checkout v0.5.0` (or later).
3. `sudo sh deploy/prod/init-secrets.sh`: creates the runtime role password, moves the old connection URL to the
   schema owner, and creates a new one for the services.
4. `sudo sh deploy/scripts/split-db-roles.sh`: creates the runtime role in the bundled PostgreSQL. It is safe to run
   again.
5. Start the stack as above.

With an external PostgreSQL, create the runtime role yourself instead of step 4
([DEPLOYMENT.md §6.1](../DEPLOYMENT.md#61-postgresql-роли-и-расширения-adr-011-adr-024-реализовано)).

The upgrade from the previous release is tested in CI on every change, together with a restore drill.
