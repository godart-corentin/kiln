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
- Atomic filesystem queue with pinned Git refs
- `systemd.path` queue activation and crash/reboot recovery
- GNU Make dependency graph and parallel execution
- Ephemeral Docker jobs with CPU/RAM/PID limits
- Dedicated CI Docker network with Internet access but private/LAN networks blocked
- Persistent per-step logs and artifacts
- Discord notifications
- CLI:
  - `kiln status`
  - `kiln logs`
  - `kiln watch`
  - `kiln rerun`
  - project create/delete/webhook management
- Optional read-only web UI behind an existing Caddy Docker deployment
- Normal `main` pushes can never activate `when: release` steps
- Initial `vX.Y.Z` tags create release jobs

## Requirements

- Ubuntu 24.04 LTS
- systemd
- Docker Engine already installed and running
- rootful Docker with a `docker` group
- Internet access during installation for Ubuntu packages
- For the optional web UI: an existing Caddy container managed with Docker Compose

Kiln **does not install or globally reconfigure Docker**.

## Fresh installation

```bash
git clone git@github.com:godart-corentin/kiln.git kiln
cd kiln

sudo ./install.sh
```

The installer creates:

```text
users:
  git
  kiln
  kiln-web

groups:
  kiln-submit
  kiln-readers

/srv/git/
/var/lib/kiln/
/etc/kiln/
/usr/local/bin/kiln
/usr/local/libexec/kiln/
/etc/systemd/system/kiln-*
```

If `172.30.0.0/24` conflicts with an existing Docker/LAN subnet, choose another subnet on first install:

```bash
sudo KILN_CI_SUBNET=172.31.50.0/24 ./install.sh
```

Then reconnect your shell if the installer added your account to `kiln-readers`.

Check the installation:

```bash
kiln doctor
```

## Add your development SSH key

On the Kiln server:

```bash
kiln git-key add
```

Paste the contents of your development machine's public Ed25519 key, for example:

```bash
cat ~/.ssh/id_ed25519.pub
```

Kiln stores it as a restricted key under:

```text
/srv/git/.ssh/authorized_keys
```

The `git` account uses `git-shell`, so an interactive SSH login is intentionally rejected.

## Create a project

```bash
kiln project create my_app
```

This automatically creates and configures:

```text
/srv/git/my_app.git
/etc/kiln/projects/my_app.json
/etc/kiln/secrets/my_app.discord-webhook
post-receive hook
Git pin namespace ACLs
read-only controller ACLs
```

On your development machine:

```bash
git remote add home git@kiln-server:/srv/git/my_app.git
git push home main
```

## Configure Discord

```bash
kiln project webhook set my_app
```

The webhook is entered without echo and never passed as a command-line argument to build containers.

## Pipeline

Commit a `.kiln/pipeline.json` file to the project.

Example:

```json
{
  "schema": 1,
  "max_parallel": 3,
  "steps": {
    "test": {
      "stage": "tests",
      "image": "node:22-bookworm",
      "network": "kiln-ci",
      "needs": [],
      "command": [
        "sh",
        "-lc",
        "corepack pnpm@11.15.1 install --frozen-lockfile && corepack pnpm@11.15.1 test"
      ]
    },
    "build": {
      "stage": "build",
      "image": "node:22-bookworm",
      "network": "kiln-ci",
      "needs": ["test"],
      "command": [
        "sh",
        "-lc",
        "corepack pnpm@11.15.1 install --frozen-lockfile && corepack pnpm@11.15.1 build && cp -a dist/. /artifacts/"
      ]
    },
    "release": {
      "stage": "release",
      "when": "release",
      "image": "node:22-bookworm",
      "network": "kiln-ci",
      "needs": ["build"],
      "command": [
        "sh",
        "-lc",
        "cp -a dist/. /artifacts/"
      ]
    }
  }
}
```

`network` may be:

- `none`: no network
- `kiln-ci`: public Internet egress; RFC1918/private networks and the Docker host are blocked

The project cannot request arbitrary Docker flags.

## CI vs release

Normal CI:

```bash
git push home main
```

Release:

```bash
git tag v1.5.0
git push home v1.5.0
```

Only the initial creation of a SemVer-style `vX.Y.Z` tag becomes a release job.

## CLI

```bash
kiln status latest
kiln status 84fb731

kiln logs latest
kiln logs latest test

kiln watch latest test
kiln watch latest pipeline

kiln rerun latest

kiln project create foo
kiln project webhook set foo
kiln project delete foo

kiln doctor
```

`kiln rerun` creates a new CI build for the same SHA. Release builds cannot be rerun from this command.

## Filesystem layout

```text
/srv/git/
  <project>.git

/etc/kiln/
  defaults.json
  network.env
  projects/
  secrets/
  web.json            # only if the web UI is installed

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
  locks/
```

## Security model

### Git

`git` owns bare repositories and is reachable via SSH, but its shell is `git-shell`.

### Controller

`kiln` cannot SSH into the machine. It has read access to project Git repositories and write access only to `refs/kiln/jobs/`, used to keep queued commits alive.

### Build containers

Build code receives:

- an isolated workspace
- `/artifacts`
- non-secret Kiln metadata
- explicit resource limits

Build code does **not** receive:

- `/var/run/docker.sock`
- `/etc/kiln/secrets`
- Git repositories
- the host root filesystem
- privileged mode or devices

### Network

`kiln-ci` uses a dedicated Docker bridge. The firewall blocks private, loopback, link-local, carrier-grade NAT, multicast and reserved IPv4 destinations. This is intended to prevent project code from accessing NAS/LAN/host services while still permitting public package registries.

Review `/etc/kiln/network.env` before deployment if your network topology is unusual.

## Read-only web UI

Kiln Web is optional. It runs as a separate container and is intended to sit behind an **existing** Dockerized Caddy instance.

```bash
sudo ./install-web.sh kiln.example.com
```

The installer:

- discovers the running `caddy` container
- discovers its Compose project and Caddyfile bind mount
- creates a separate `/opt/kiln/docker-compose.yml`
- creates an internal shared Docker network named `kiln-proxy`
- writes a small Caddy Compose override so Caddy retains that network
- adds a Basic Auth protected Kiln block to the existing Caddyfile
- backs up the affected files before modification
- publishes no Kiln Web port on the host

The UI is read-only. It shows builds, steps, logs and artifact names.

Discord notifications use `/etc/kiln/web.json` to include a direct build link.

Remove only the web layer with:

```bash
sudo ./uninstall-web.sh
```

## Updating Kiln

Pull the repository and run:

```bash
git pull
./update.sh
```

The update script reinstalls trusted programs and systemd units but does not delete repositories, project configuration, secrets or build history.

## Uninstalling the core

```bash
sudo ./uninstall.sh
```

The uninstaller removes the programs, systemd units and the CI Docker network/firewall rules.

It deliberately preserves:

```text
/srv/git
/var/lib/kiln
/etc/kiln
```

so an uninstall cannot casually destroy repositories, secrets or build history.

## Developing Kiln

Run static checks:

```bash
./tests/run.sh
```

The repository intentionally contains no GitHub Actions workflow. GitHub is used only to distribute and version Kiln itself; Kiln does not depend on GitHub Actions.
