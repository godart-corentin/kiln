# Contributing to Kiln

Kiln is intentionally small infrastructure software built around standard Unix tools.

Contributions should preserve that philosophy:

- simplicity over abstraction;
- standard Unix tools over large dependencies;
- explicit behavior over magic;
- security boundaries over convenience;
- reproducibility over implicit host state.

## Development Requirements

The primary target is Ubuntu 24.04 LTS.

Kiln currently relies on:

- Python 3 standard library;
- Bash;
- Git;
- GNU Make;
- systemd;
- Docker Engine;
- Linux ACLs;
- iptables.

Avoid adding runtime dependencies unless there is a clear benefit.

## Running Checks

Run:

    ./tests/run.sh

Before submitting a change, also verify:

    bash -n install.sh
    bash -n update.sh
    bash -n uninstall.sh
    bash -n install-web.sh
    bash -n uninstall-web.sh

Python scripts should pass the static checks included in the test suite.

## Security-Sensitive Changes

Extra care is required when modifying:

- `libexec/controller`;
- `libexec/execute`;
- Git hooks;
- repository permissions or ACLs;
- Docker arguments;
- CI networking or firewall rules;
- secret handling;
- project creation or deletion;
- web exposure or authentication.

Repository-controlled values must never become arbitrary host shell commands.

Build containers must never receive the Docker socket or Kiln secrets.

## Project Configuration

Do not add project-specific configuration to the Kiln repository.

Kiln itself must remain generic.

Project runtime state belongs under:

    /etc/kiln/
    /var/lib/kiln/
    /srv/git/

These directories contain installation-specific state and must not be copied into the Kiln source repository.

## Pull Requests

Keep changes focused.

For behavioral changes:

1. explain the problem being solved;
2. describe security implications when relevant;
3. include tests or reproducible verification;
4. update documentation if user-facing behavior changes.

## Releases

Kiln uses semantic-style version tags such as:

    v0.1.0
    v0.1.1
    v0.2.0

Update `VERSION` and `CHANGELOG.md` when preparing a release.
