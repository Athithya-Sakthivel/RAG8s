# CNPG backup and restore

This workflow is built around one stable backup lineage and one stable S3 namespace. The goal is simple: deploy once, take backups repeatedly, and restore into a fresh Kubernetes cluster when needed, without renaming the lineage every time.

## Mental model

There are three separate identities.

`K8S_CLUSTER` is the Kubernetes platform mode used by the script, such as `kind`.

`PG_CLUSTER_ID` scopes the S3 backup prefix. It is the namespace for the backup archive, and it must stay stable for one lineage.

`PG_SERVER_NAME` is the lineage name written into CloudNativePG’s Barman backup configuration. It is the stable backup identity for deploys and backups.

The restore target lineage is generated internally by the script. Do not hardcode it in Makefiles or shell blocks.

## First deploy

The first deploy creates the PostgreSQL cluster and the base databases.

```bash
export K8S_CLUSTER=kind
export PG_BACKUPS_S3_BUCKET=e2e-mlops-data-681802563986
export PG_CLUSTER_ID=cnpg-cluster-kind
export PG_SERVER_NAME=mlsecops

make pg-cluster

```

For the first deploy, `CREATE_INITIAL_BACKUP` should remain off. The cluster comes up first; backups happen later.

What this does:

1. installs or verifies the CloudNativePG operator,
2. creates the PostgreSQL cluster,
3. waits for readiness,
4. creates the application databases,
5. configures the pooler,
6. enables continuous archiving.

## Backup later

Once the cluster has data, take a physical base backup.

```bash
export K8S_CLUSTER=kind
export PG_BACKUPS_S3_BUCKET=e2e-mlops-data-681802563986
export PG_CLUSTER_ID=cnpg-cluster-kind
export PG_SERVER_NAME=mlsecops

make pg-backup
```

This creates a new base backup and continues WAL archiving under the same lineage.

The backup layout is:

```text
s3://$PG_BACKUPS_S3_BUCKET/postgres_backups/$PG_CLUSTER_ID/$PG_SERVER_NAME/
```

A backup is not just one file. It is a base backup plus the WAL stream that follows it.

## Restore latest into a new Kubernetes cluster

Restores must run into a fresh Kubernetes cluster. Create the new cluster first, then restore into it.

```bash
make core

export K8S_CLUSTER=kind
export PG_BACKUPS_S3_BUCKET=e2e-mlops-data-681802563986
export PG_CLUSTER_ID=cnpg-cluster-kind
export PG_SERVER_NAME=mlsecops

make pg-restore-latest
```

The restore uses the existing lineage as the source. The target lineage name is generated inside the script so it will not collide with the source archive.

`RESTORE_SOURCE_SERVER_NAME` is optional here because it defaults to `PG_SERVER_NAME`. Set it only when restoring from a different existing lineage.

## Restore to a timestamp into a new Kubernetes cluster

Point-in-time recovery works the same way: create a fresh Kubernetes cluster first, then restore into it.

```bash
make core

export K8S_CLUSTER=kind
export PG_BACKUPS_S3_BUCKET=e2e-mlops-data-681802563986
export PG_CLUSTER_ID=cnpg-cluster-kind
export PG_SERVER_NAME=mlsecops
export RESTORE_SOURCE_SERVER_NAME=mlsecops
export TARGET_TIME=2026-04-12T01:02:20Z

make pg-restore-time
```

`TARGET_TIME` must be RFC3339.
It must fall within the WAL coverage that exists for the chosen lineage.

A valid PITR target is not “any time after the backup.” It must also be reachable from the archived WAL stream. If the target is later than the last recoverable WAL event, CNPG will replay everything it can and then fail with a recovery-target error.

## How restore works internally

Restore is a recovery bootstrap, not a normal cluster creation.

The script:

1. reads the source lineage from `PG_SERVER_NAME` or `RESTORE_SOURCE_SERVER_NAME`,
2. verifies that the source lineage has a completed base backup,
3. generates a fresh restore target server name,
4. renders a recovery-based Cluster manifest,
5. points `externalClusters` to the source lineage,
6. sets `recoveryTarget.targetTime` only for PITR,
7. applies the manifest and waits for recovery to finish,
8. brings up the pooler and prints connection URIs.

For latest restore, CNPG restores the latest usable base backup and then replays WAL up to the latest available point.

For PITR, CNPG restores the same base backup and replays WAL only up to the requested timestamp.

## Failure rules and what they mean

If deploy fails, check whether the S3 prefix is empty for the requested lineage. Fresh deploys must not reuse an archive path that already contains objects unless that is intentional.

If backup fails, confirm that the cluster is healthy and that continuous archiving is enabled.

If restore-latest fails, verify that the source lineage has a completed base backup under the expected S3 prefix.

If PITR fails with a message like “recovery ended before configured recovery target was reached,” the target timestamp is too late for the WAL that exists in the archive. Move the target earlier.

If PITR fails before recovery even begins, the source lineage, bucket path, or credentials are wrong.

## Contracts and invariants

These rules should not change:

`PG_SERVER_NAME` is the stable lineage name.
`PG_CLUSTER_ID` is the stable S3 namespace for that lineage.
`K8S_CLUSTER` identifies the platform mode.
`CREATE_INITIAL_BACKUP` applies only to the first deploy.
Restore target names are generated by the script.
Restore must run into a fresh Kubernetes cluster.
The source lineage for restore must already have a completed base backup.

## What not to do

Do not use `RESTORE_SERVER_NAME=auto` unless the script explicitly accepts it.
Do not change `PG_CLUSTER_ID` when restoring from the same backup lineage.
Do not try to restore into the same Kubernetes cluster object after a reset.
Do not treat a WAL timestamp as equivalent to a backup object. The restore chain is base backup plus WAL replay.

## Typical flow

First deploy:

```bash
make pg-cluster
```

Later backup:

```bash
make pg-backup
```

After a cluster reset:

```bash
make core
make pg-restore-latest
```

For PITR:

```bash
make core
export TARGET_TIME=2026-04-12T01:02:20Z
make pg-restore-time
```

## Finding the correct PITR timestamp

Use a timestamp that is earlier than the last recoverable event in the WAL archive. The most reliable method is to insert a marker row, record the exact UTC time, and restore to just before that time. That gives you a deterministic before/after boundary.

For example:

1. restore latest
2. insert a test row and record `now()`
3. make another change
4. restore to one second before the recorded time

That is the cleanest way to test PITR without guessing.
