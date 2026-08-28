# Changelog

All notable changes to Kilnr will be documented in this file.

Kilnr is currently experimental 0.x software.

## Unreleased

### Added

### Changed

### Fixed

### Security

## 0.1.0 - 2026-08-26

### Added

- Bare Git repositories over restricted SSH.
- Atomic filesystem CI queue.
- Exact-SHA builds with temporary Git pin refs.
- systemd queue activation.
- GNU Make dependency graph execution.
- Parallel CI steps with dependency failure propagation.
- Ephemeral Docker build containers.
- CPU, memory, PID, capability and privilege restrictions.
- Dedicated CI Docker network with private and LAN egress blocking.
- Persistent build logs and artifacts.
- Discord build notifications.
- `kilnr status`, `kilnr logs`, `kilnr watch` and `kilnr rerun`.
- Project create, delete and Discord webhook management.
- Optional read-only Kilnr Web interface behind Caddy.
- Release jobs triggered only from initial `vX.Y.Z` tag creation.
