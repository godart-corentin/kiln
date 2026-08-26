# Security Policy

Kiln is CI infrastructure and executes repository-controlled code.

A Kiln installation should therefore be treated as privileged infrastructure even though individual build containers are deliberately restricted.

## Supported Versions

Kiln is currently experimental 0.x software.

Security fixes are provided for the latest version only.

| Version | Supported |
| --- | --- |
| Latest 0.x | Yes |
| Older versions | No |

## Reporting a Vulnerability

Do not report security vulnerabilities in public GitHub issues.

Please use GitHub's private vulnerability reporting feature:

**Security → Report a vulnerability**

If private vulnerability reporting is unavailable, contact the maintainer through a private channel rather than publishing exploit details.

Please include, when possible:

- the affected Kiln version or commit;
- the relevant component;
- reproduction steps;
- expected and actual behavior;
- security impact;
- any suggested mitigation.

## Security Model

Kiln deliberately separates several trust domains:

- `git` owns bare repositories and receives SSH pushes;
- `kiln` controls CI state and orchestration;
- build containers receive repository code but not the Docker socket;
- `/etc/kiln/secrets` is never mounted into build containers;
- Kiln Web is read-only and runs separately from the controller;
- CI containers use resource limits and a dedicated network;
- private/LAN networks are blocked from the default CI network.

The Docker daemon itself remains privileged infrastructure. The Kiln controller's Docker access must therefore be treated accordingly.

## Secrets

Never commit:

- Discord webhooks;
- SSH private keys;
- Apple signing certificates or credentials;
- tokens;
- passwords;
- `/etc/kiln/secrets` contents;
- private runtime configuration copied from a Kiln server.

If a secret is ever committed to Git, removing the file from a later commit is not sufficient. Rotate or revoke the secret.

## Build Containers

Repository-controlled build containers must not receive:

- `/var/run/docker.sock`;
- `--privileged`;
- host PID/network namespaces;
- arbitrary host devices;
- Kiln secrets;
- unrestricted access to private/LAN networks.

Changes which weaken these boundaries should be treated as security-sensitive.
