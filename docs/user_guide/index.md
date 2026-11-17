# User guide

The user guide covers each area of the package by topic. Each page stands on its own; read
{doc}`configuration` first if you're new.

```{toctree}
:maxdepth: 2

configuration
connections
assess
logical
sync
lift
validation
performance
openshift
security
troubleshooting
limitations
```

| Page | Read it when you want to… |
|---|---|
| {doc}`configuration` | understand the config file, secrets and command-line overrides |
| {doc}`connections` | choose a driver, set up TLS, reach a server through OpenShift |
| {doc}`assess` | find what won't work on SQL Server on Linux / OpenShift before you migrate |
| {doc}`logical` | understand chunking, identity handling, resume and scale-out |
| {doc}`sync` | keep the application running during a logical migration and cut over in minutes |
| {doc}`lift` | migrate tens of TB with minutes of downtime |
| {doc}`validation` | prove the copy is correct, and read the report |
| {doc}`performance` | make it faster: workers, stripes, storage, network |
| {doc}`openshift` | run it inside the cluster as Jobs |
| {doc}`security` | handle permissions, secrets, TLS and logins safely |
| {doc}`troubleshooting` | fix a specific error message |
| {doc}`limitations` | know what is not migrated |
