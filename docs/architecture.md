# Architecture

## Push path

```text
developer
   │ git push
   ▼
sshd → git-shell
   ▼
/srv/git/<project>.git
   ▼ post-receive
enqueue
   ├─ classifies ci/release
   ├─ resolves exact commit SHA
   ├─ creates refs/kiln/jobs/<job-id>
   └─ atomically publishes job.json
          ▼
/var/lib/kiln/queue/incoming
          ▼ systemd.path
kiln-controller.service
          ▼
controller
   ├─ claims job into queue/running
   ├─ archives exact SHA into build/src
   ├─ reads .kiln/pipeline.json from that SHA
   ├─ validates the pipeline
   ├─ generates trusted pipeline.mk
   └─ launches GNU Make
          ▼
make -jN -k
          ▼
execute <build> <step>
          ▼
ephemeral Docker container
```

## Why Make

Kiln does not implement a general-purpose scheduler. Each pipeline step becomes a Make target. `needs` becomes target prerequisites. GNU Make handles readiness, concurrency and dependency failure propagation.

After Make finishes, Kiln converts remaining `pending` steps into `skipped`.

## Exact SHA

The hook never schedules `main` or a tag name as a build source. It stores the resolved commit SHA. An internal `refs/kiln/jobs/<id>` ref keeps the object reachable until the build has been prepared.

The workspace is produced with `git archive <sha>`, so it contains no `.git` directory.

## Trust boundaries

### `git`

Owns the bare repositories and accepts restricted SSH Git traffic. It can submit jobs but cannot read Kiln secrets.

### `kiln`

Runs the controller. It can read bare repositories and write only the `refs/kiln/jobs` namespace. The systemd controller receives Docker group access through `SupplementaryGroups=docker`; the account is not permanently added to the Docker group.

### build containers

Repository-controlled commands run only inside Docker. They do not receive the Docker socket or Kiln secrets.

### `kiln-web`

The optional web interface only reads build output. It runs in a separate Docker container behind Caddy and receives no host port.

## Queue and crashes

Job publication and status writes use temporary files plus atomic renames.

At controller startup:

- a job claimed into `running/` before its build directory existed is returned to `incoming/`;
- an interrupted build with an existing build directory is marked `aborted`;
- labeled Docker containers from an interrupted build are force-removed;
- the job pin is cleaned up.

## Releases

Only the initial creation of a tag matching `^v[0-9]+\.[0-9]+\.[0-9]+$` becomes a `release` job.

Pipeline steps with `"when": "release"` are excluded entirely from normal CI runtime data.

## Current limitation

Kiln 0.1 executes project steps in Linux Docker containers. Native macOS workers are intentionally not part of this initial package.
