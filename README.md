# Kiln

Kiln is a small self-hosted CI system for Ubuntu 24.04 LTS built from standard Unix tools:

```text
git push
   ↓
bare Git repository
   ↓
post-receive
   ↓
filesystem queue
   ↓
systemd
   ↓
GNU Make DAG
   ↓
ephemeral Docker containers
```

Kiln deliberately has no Jenkins/GitLab/GitHub Actions server, no database, and no Docker socket inside build containers.

> [!WARNING]
> Kiln is experimental 0.x software. It executes repository-controlled code and integrates with Docker, systemd, Git, and host firewall rules. Review the security model before using it with untrusted repositories or exposing Kiln services publicly.

## Current features

- Bare Git repositories over SSH using a restricted `git` account
- Exact-SHA builds; branch names are never used for checkout
- Atomic filesystem queue with pinned Git refs under `refs/kiln/jobs/`
- Branch pipelines from `.kiln/pipelines/*.json`
- Release pipelines from `.kiln/release.json`
- GNU Make dependency graph with groups, `needs`, and parallel jobs
- `run`, `script`, and low-level `command` execution modes
- Managed `pnpm` via `tools`, resolved from the exact-SHA `package.json`
- Persistent pnpm store cache with `cache: ["pnpm"]`
- Persistent per-job logs and artifacts
- Artifact inputs between dependent jobs
- Project-scoped release secrets
- Ephemeral Docker jobs with CPU/RAM/PID limits and hardened flags
- Dedicated CI Docker network with Internet access but private/LAN destinations blocked
- Discord notifications
- Read-only web UI with JSON API and SSE updates
- CLI for status, logs, watch, rerun, projects, secrets, webhooks, Git keys, and diagnostics

## Requirements

- Ubuntu 24.04 LTS
- systemd
- Docker Engine already installed and running
- rootful Docker with a `docker` group
- Internet access during installation for Ubuntu packages
- For the optional web UI: an existing Caddy container managed with Docker Compose

Kiln **does not install or globally reconfigure Docker**.

## Installation

```bash
git clone git@github.com:godart-corentin/kiln.git kiln
cd kiln
sudo ./install.sh
```

If `172.30.0.0/24` conflicts with an existing Docker/LAN subnet, choose another subnet on first install:

```bash
sudo KILN_CI_SUBNET=172.31.50.0/24 ./install.sh
```

Then check the installation:

```bash
kiln doctor
```

## SSH key and project creation

Add a development key on the Kiln server:

```bash
kiln git-key add
```

Create a project:

```bash
kiln project create my_app
```

This creates the bare repository, project configuration, secret directory, post-receive hook, and exact-SHA pin namespace.

On the development machine:

```bash
git remote add home git@kiln-server:/srv/git/my_app.git
git push home main
```

Configure Discord if wanted:

```bash
kiln project webhook set my_app
```

## Branch pipelines

Branch CI lives in `.kiln/pipelines/*.json`. A branch push scans the pipeline files from the exact pushed SHA. Zero matching pipelines means no build; exactly one runs; multiple matches are a configuration error.

Example:

```json
{
  "schema": 1,
  "trigger": {
    "type": "branch",
    "branches": ["*"]
  },
  "max_parallel": 4,
  "jobs": {
    "tests": {
      "group": "quality",
      "image": "node:24-bookworm",
      "network": "kiln-ci",
      "tools": ["pnpm"],
      "cache": ["pnpm"],
      "run": [
        "pnpm install --frozen-lockfile",
        "pnpm test"
      ]
    },
    "build": {
      "group": "quality",
      "image": "node:24-bookworm",
      "network": "kiln-ci",
      "tools": ["pnpm"],
      "cache": ["pnpm"],
      "run": [
        "pnpm install --frozen-lockfile",
        "pnpm build"
      ],
      "artifacts": ["dist/**"]
    }
  }
}
```

`run` commands execute sequentially in the same `/bin/sh -eu` process. Jobs run in parallel when the DAG allows it, up to `max_parallel`.

`network` may be:

- `none`: no network
- `kiln-ci`: public Internet egress while private/LAN/host ranges are blocked

The project cannot request arbitrary Docker flags.

## Dependencies and groups

`needs` is the sole source of DAG ordering. It can reference either a job or a group:

```json
{
  "jobs": {
    "lint": {
      "group": "quality",
      "image": "node:24-bookworm",
      "tools": ["pnpm"],
      "run": ["pnpm lint"]
    },
    "tests": {
      "group": "quality",
      "image": "node:24-bookworm",
      "tools": ["pnpm"],
      "run": ["pnpm test"]
    },
    "package": {
      "needs": ["quality"],
      "image": "node:24-bookworm",
      "tools": ["pnpm"],
      "run": ["pnpm build"]
    }
  }
}
```

A group is only an organizational and dependency shortcut; it does not execute itself.

## Managed tools

Kiln currently supports `pnpm` as a managed tool.

Automatic version resolution:

```json
"tools": ["pnpm"]
```

requires the exact-SHA root `package.json` to contain, for example:

```json
"packageManager": "pnpm@11.15.1"
```

An explicit version is also supported:

```json
"tools": {
  "pnpm": "11.15.1"
}
```

Kiln exposes the managed binary inside the container; pipeline commands simply use `pnpm`.

## Persistent pnpm cache

Enable the project-scoped persistent pnpm store for a job with:

```json
"tools": ["pnpm"],
"cache": ["pnpm"]
```

Each job still runs the normal deterministic install:

```bash
pnpm install --frozen-lockfile
```

but pnpm can reuse packages already present in the warm store instead of downloading them again.

Kiln stores the cache under:

```text
/var/lib/kiln/cache/<project>/<job-type>/pnpm/<version>/
```

and mounts only that store into the job at:

```text
/run/kiln/cache/pnpm
```

Important properties:

- projects never share caches
- normal CI and release jobs never share caches
- pnpm versions never share caches
- the cache is an accelerator, not a source of build truth
- the exact lockfile and `pnpm install --frozen-lockfile` remain authoritative
- workspaces remain isolated; `node_modules` is not shared between jobs

## Artifacts and inputs

A job can persist selected workspace files:

```json
"artifacts": [
  "release/*.AppImage",
  "release/latest-linux.yml"
]
```

Declared artifact patterns that match nothing fail the job. Paths are validated so absolute paths, `..`, and symlink escapes cannot leave the workspace.

A dependent job can consume artifacts from producer jobs with `inputs`:

```json
"needs": ["package-linux"],
"inputs": ["package-linux"]
```

Producer artifacts are exposed read-only under `/run/kiln/inputs/<producer>` with a matching `KILN_INPUT_<PRODUCER>` environment variable.

## Release pipelines and secrets

Release jobs live only in `.kiln/release.json`. Branch CI never loads that file.

A SemVer-style tag such as `v1.5.0` creates a release build:

```bash
git tag v1.5.0
git push home v1.5.0
```

Project-scoped release secrets can be managed with:

```bash
kiln secret set my_app APPLE_ID
kiln secret set-file my_app WIN_CSC_LINK ./certificate.pfx
kiln secret list my_app
kiln secret delete my_app APPLE_ID
```

Secrets are release-only by default, mounted read-only for the requesting job, omitted from Docker environment metadata, and known textual values are redacted from persisted logs.

## Automatic job environment

Kiln provides non-secret metadata including:

```text
KILN_BUILD_ID
KILN_PROJECT
KILN_SHA
KILN_REF
KILN_JOB_TYPE
KILN_JOB
KILN_BRANCH   # branch CI
KILN_TAG      # release
```

User-defined environment variables can be added with `env`, but `KILN_*` is reserved.

## CLI

Typical commands:

```bash
kiln status latest
kiln logs latest
kiln logs latest tests
kiln watch latest pipeline
kiln rerun latest

kiln project create foo
kiln project webhook set foo
kiln project delete foo

kiln secret list foo
kiln doctor
```

`kiln rerun` creates a new CI build for the same SHA. Release builds are not rerun by that command.

## Filesystem layout

```text
/srv/git/
  <project>.git

/etc/kiln/
  defaults.json
  network.env
  projects/
  secrets/
  web.json

/var/lib/kiln/
  queue/
    tmp/
    incoming/
    running/
  builds/
    <build-id>/
      job.json
      runtime.json
      status.json
      pipeline.mk
      src/
      work/
      logs/
      artifacts/
  cache/
    <project>/
      ci/
        pnpm/
          <version>/
      release/
        pnpm/
          <version>/
  job-runtime/
  secret-staging/
  locks/
```

## Security model

### Git

`git` owns bare repositories and is reachable via SSH, but its shell is `git-shell`.

### Controller

`kiln` has no login shell. It reads repositories through ACLs and can write only Kiln's pinned job refs inside each bare repository.

### Build containers

Every job gets a fresh exact-SHA workspace. Docker jobs run with resource limits, all Linux capabilities dropped, `no-new-privileges`, and a non-root UID/GID.

Build code does **not** receive:

- `/var/run/docker.sock`
- the host root filesystem
- bare Git repositories or `.git`
- arbitrary host devices
- privileged mode
- unrelated project secrets or caches

`/tmp` is a no-exec tmpfs. Executable temporary work uses `/run/kiln/tmp`, while `HOME=/run/kiln/home` is an ephemeral disk-backed per-job directory removed after the job.

### Network

`kiln-ci` uses a dedicated Docker bridge. Host firewall rules block private, loopback, link-local, carrier-grade NAT, multicast, and reserved IPv4 destinations while permitting public Internet package access.

## Read-only web UI

Kiln Web is optional and runs separately behind an existing Dockerized Caddy deployment:

```bash
sudo ./install-web.sh kiln.example.com
```

It publishes no Kiln Web host port, uses the shared internal `kiln-proxy` network, and keeps Basic Auth at Caddy. The web process is read-only against Kiln build state.

Remove only the web layer with:

```bash
sudo ./uninstall-web.sh
```

## Updating Kiln

```bash
git pull --ff-only
sudo ./update.sh
./tests/run.sh
kiln doctor
```

The update path preserves repositories, project configuration, secrets, caches, and build history.

## Uninstalling

```bash
sudo ./uninstall.sh
```

The uninstaller removes the installed programs, systemd units, and CI network/firewall setup while deliberately preserving persistent Kiln data under `/srv/git`, `/var/lib/kiln`, and `/etc/kiln`.

## Development

Run the repository test suite with:

```bash
./tests/run.sh
```

Kiln intentionally contains no GitHub Actions workflow. GitHub is used for source hosting and versioning only; Kiln itself performs CI.