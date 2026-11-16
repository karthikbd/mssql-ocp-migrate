# Running on OpenShift

Everything under `deploy/openshift/` is a starting point. Change namespaces, image names and
storage classes to match your cluster.

## The image

```console
$ python -m pip wheel . -w dist --no-deps
$ podman build -f deploy/openshift/Containerfile -t mssql-ocp-migrate:{{ version }} .
```

The `Containerfile` installs the wheel with `pip install --no-index`, so the build needs no network
and the image contains only UBI Python and this package. It runs as a non-root user and works with
OpenShift's random UIDs.

## Configuration and secrets

```console
$ oc create secret generic mssql-migrate-creds \
    --from-literal=source-password='…' --from-literal=target-password='…'
$ oc apply -f deploy/openshift/configmap.yaml       # migrate.toml as a ConfigMap
```

The ConfigMap's `migrate.toml` reads passwords with `password_file = "/secrets/<key>"`.

## Logical copy as a Job

`deploy/openshift/job.yaml` runs `migrate` with a small PVC for the state file and report. The
example ConfigMap sets `if_target_not_empty = "resume"`, so a Job retry continues where the last
attempt stopped.

```console
$ oc apply -f deploy/openshift/job.yaml
$ oc logs -f job/mssql-migrate
$ oc cp <pod>:/work/data/report/report.html ./report.html
```

## Scale-out: an Indexed Job

`deploy/openshift/scale-out/indexed-job.yaml` runs N pods (`completionMode: Indexed`). Each one
runs `migrate --shard auto/N`, and the shard index comes from `JOB_COMPLETION_INDEX`. All pods
share a ReadWriteMany volume holding `plan.json` and the per-shard state files. When the Job
completes:

```console
$ mssql-migrate finalize -c /config/migrate.toml --shard-count 16
```

## Physical lift

| File | Purpose |
|---|---|
| `lift/01-backup-share.yaml` | an NFS PersistentVolume and Claim for the backup share (ReadWriteMany, `nconnect=16`), sized for the compressed full backup plus a few days of logs |
| `lift/02-sqlserver-volumes-patch.yaml` | a patch for the SQL Server StatefulSet: 4 data PVs, a log PV and the share mount, so restore writes run in parallel |
| `lift/03-lift-job.yaml` | the long-running Job: `lift run --follow-hours 168` (seed, then keep current for up to 7 days); progress lives on a PVC |
| `lift/04-ag-endpoint-service.yaml` | Option B only: exposes port 5022 for availability-group seeding |

The lift Job only sends T-SQL, so 100 millicores and 256 MiB are enough. The servers do the heavy
lifting. **The SQL Server pod** needs CPU for decompression and fast volumes.

Cut-over in the maintenance window:

```console
$ oc delete job mssql-lift                       # stop following
$ oc run mssql-cutover --rm -it --restart=Never --image=<image> -- \
      lift cutover -c /config/migrate.toml --confirm
$ oc run mssql-verify --rm -it --restart=Never --image=<image> -- \
      lift verify -c /config/migrate.toml
```

(Mount the same ConfigMap, Secret and work PVC into these pods as in `03-lift-job.yaml`.)

## Network checklist

- **Source → share:** the source SQL Server service account can write to the share
  (`source_backup_dir`).
- **SQL Server pod → share:** the share is mounted read-write at `target_backup_dir`.
- **Tool pod → both servers:** TCP 1433 (or your ports). The source must be reachable from the
  cluster (egress rules, firewalls).
- **AG seeding only:** source ↔ pod on TCP 5022, both directions.
