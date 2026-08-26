# Persistent pnpm Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a project/job-type/pnpm-version scoped persistent pnpm store cache that can be enabled per job with `"cache": ["pnpm"]`.

**Architecture:** Pipeline parsing validates `cache` independently from `tools`, then normalizes enabled caches to the resolved tool version. The executor creates host cache directories under `/var/lib/kiln/cache/<project>/<job-type>/pnpm/<version>`, bind-mounts them at `/run/kiln/cache/pnpm`, and configures the managed pnpm wrapper to use that store. Cache data never becomes workspace data and branch/release caches remain isolated.

**Tech Stack:** Python 3 stdlib, Docker CLI, pnpm/Corepack, Bash installer/tests.

**Spec:** Validated in chat: persistent cache first; v1 supports pnpm only; no `node_modules` sharing yet; branch/release/project/version isolation; CLI cleanup is a separate follow-up.

## Global Constraints

- Pipeline schema remains `schema: 1`.
- `cache` input syntax is a list, initially only `"pnpm"`.
- `cache: ["pnpm"]` requires a resolved pnpm tool version.
- Cache path is project + job type + tool + exact tool version scoped.
- CI and release caches must never share storage.
- Cache mount is writable, but workspace and all existing security restrictions remain unchanged.
- No Docker socket, privileged mode, host PID, devices, or global Docker reconfiguration.

---

### Task 1: Add cache schema tests and validation

**Files:**
- Create: `tests/test_cache.py`
- Modify: `tests/run.sh`
- Modify: `libexec/pipeline.py`

**Interfaces:**
- Consumes: resolved `job["tools"]` mapping.
- Produces: normalized `job["cache"]` mapping `{tool: version}`.

- [ ] Add failing tests for accepted pnpm cache, missing tool, duplicate/unsupported cache names, and project/job-type/version cache key isolation.
- [ ] Run the cache tests and verify they fail for missing cache support.
- [ ] Add `_validate_cache()` to `pipeline.py` and include normalized cache metadata in each job.
- [ ] Run cache and pipeline tests.

### Task 2: Mount and configure the persistent pnpm store

**Files:**
- Modify: `libexec/execute`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `runtime["project"]`, `runtime["job_type"]`, normalized `job["cache"]`.
- Produces: writable bind mount at `/run/kiln/cache/pnpm`; managed pnpm wrapper uses that path as `store-dir`.

- [ ] Add failing executor tests for cache root path, writable mount, and managed pnpm configuration.
- [ ] Run tests and verify they fail.
- [ ] Add cache root preparation and mount generation.
- [ ] Configure only the managed pnpm wrapper to use `/run/kiln/cache/pnpm` when enabled.
- [ ] Re-run executor/cache tests.

### Task 3: Install cache root and document usage

**Files:**
- Modify: `install.sh`
- Modify: `README.md`
- Test: `tests/test_cache.py`

**Interfaces:**
- Produces: `/var/lib/kiln/cache` owned by `kiln:kiln`, mode `0700`.

- [ ] Add installation/static tests for the cache root.
- [ ] Add the cache directory to `install.sh`.
- [ ] Document `cache: ["pnpm"]`, isolation, and the fact that each job still runs `pnpm install --frozen-lockfile` against a warm shared store.
- [ ] Run `./tests/run.sh`, Python syntax checks, shell syntax checks, and `git diff --check` before merge.
