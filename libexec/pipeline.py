#!/usr/bin/env python3
import fnmatch
import json
import re
from pathlib import PurePosixPath

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
TOOL_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
SUPPORTED_TOOLS = {"pnpm"}


class PipelineError(ValueError):
    pass


def fail(message: str) -> None:
    raise PipelineError(message)


def _validate_string_list(value, *, label: str, max_items: int = 64, allow_empty: bool = True):
    if not isinstance(value, list) or len(value) > max_items:
        fail(f"{label} invalid")
    if not allow_empty and not value:
        fail(f"{label} invalid")
    normalized = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            fail(f"{label} invalid")
        normalized.append(item)
    return normalized


def _validate_trigger(trigger, *, kind: str):
    if kind == "release":
        if trigger is not None:
            fail("release pipeline must not define trigger")
        return None
    if kind != "ci":
        fail(f"unsupported pipeline kind: {kind!r}")
    if not isinstance(trigger, dict):
        fail("pipeline.trigger must be an object")
    if trigger.get("type") != "branch":
        fail("pipeline.trigger.type must be 'branch'")
    branches = _validate_string_list(
        trigger.get("branches"),
        label="pipeline.trigger.branches",
        max_items=64,
        allow_empty=False,
    )
    for pattern in branches:
        if len(pattern) > 255 or pattern.startswith("refs/"):
            fail(f"pipeline.trigger.branches invalid pattern {pattern!r}")
    if len(set(branches)) != len(branches):
        fail("pipeline.trigger.branches contains duplicates")
    return {"type": "branch", "branches": branches}


def _validate_script(value, *, job_name: str):
    if not isinstance(value, str) or not value or "\x00" in value:
        fail(f"job {job_name!r}: invalid script")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.endswith("/"):
        fail(f"job {job_name!r}: invalid script")
    return value


def _validate_env(value, *, job_name: str):
    if value is None:
        return {}
    if not isinstance(value, dict):
        fail(f"job {job_name!r}: invalid env")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not ENV_RE.fullmatch(key):
            fail(f"job {job_name!r}: invalid environment variable name {key!r}")
        if key.startswith("KILN_"):
            fail(f"job {job_name!r}: reserved environment variable {key!r}")
        if not isinstance(item, str) or "\x00" in item:
            fail(f"job {job_name!r}: invalid environment variable {key!r}")
        result[key] = item
    return result


def _validate_artifacts(value, *, job_name: str):
    patterns = _validate_string_list(
        value if value is not None else [],
        label=f"job {job_name!r}: invalid artifacts",
        max_items=128,
    )
    seen = set()
    for pattern in patterns:
        path = PurePosixPath(pattern)
        if path.is_absolute() or ".." in path.parts or pattern.endswith("/"):
            fail(f"job {job_name!r}: invalid artifact pattern {pattern!r}")
        if pattern in seen:
            fail(f"job {job_name!r}: duplicate artifact pattern {pattern!r}")
        seen.add(pattern)
    return patterns


def _package_manager_version(package_manager: str | None, tool: str, *, job_name: str) -> str:
    if not isinstance(package_manager, str) or not package_manager.startswith(tool + "@"):
        fail(
            f"job {job_name!r}: tools requests {tool!r} but package.json "
            f"packageManager must declare {tool}@<version>"
        )
    version = package_manager[len(tool) + 1:].split("+", 1)[0]
    if not TOOL_VERSION_RE.fullmatch(version):
        fail(f"job {job_name!r}: invalid {tool} version in packageManager")
    return version


def _validate_tools(value, *, job_name: str, package_manager: str | None):
    if value is None:
        return {}

    if isinstance(value, list):
        names = _validate_string_list(
            value, label=f"job {job_name!r}: invalid tools", max_items=8
        )
        result = {}
        for name in names:
            if name in result:
                fail(f"job {job_name!r}: duplicate tool {name!r}")
            if name not in SUPPORTED_TOOLS:
                fail(f"job {job_name!r}: unsupported tool {name!r}")
            result[name] = _package_manager_version(
                package_manager, name, job_name=job_name
            )
        return result

    if not isinstance(value, dict):
        fail(f"job {job_name!r}: invalid tools")

    result = {}
    for name, version in value.items():
        if name not in SUPPORTED_TOOLS:
            fail(f"job {job_name!r}: unsupported tool {name!r}")
        if not isinstance(version, str) or not TOOL_VERSION_RE.fullmatch(version):
            fail(f"job {job_name!r}: invalid {name} version")
        result[name] = version
    return result


def _validate_secrets(value, *, job_name: str):
    names = _validate_string_list(
        value if value is not None else [],
        label=f"job {job_name!r}: invalid secrets",
        max_items=64,
    )
    seen = set()
    for name in names:
        if not ENV_RE.fullmatch(name) or name.startswith("KILN_"):
            fail(f"job {job_name!r}: invalid secret name {name!r}")
        if name in seen:
            fail(f"job {job_name!r}: duplicate secret {name!r}")
        seen.add(name)
    return names


def _normalize_job(
    name: str,
    spec: dict,
    *,
    allowed_networks: tuple[str, ...],
    package_manager: str | None,
):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        fail(f"invalid job name: {name!r}")
    if not isinstance(spec, dict):
        fail(f"job {name!r} must be an object")

    image = spec.get("image")
    if not isinstance(image, str) or not image or len(image) > 255 or any(ord(c) < 32 for c in image):
        fail(f"job {name!r}: invalid image")

    modes = [mode for mode in ("run", "script", "command") if mode in spec]
    if len(modes) != 1:
        fail(f"job {name!r} must define exactly one of run, script, command")

    mode = modes[0]
    execution = None
    if mode == "run":
        execution = _validate_string_list(
            spec["run"], label=f"job {name!r}: invalid run", allow_empty=False
        )
    elif mode == "script":
        execution = _validate_script(spec["script"], job_name=name)
    else:
        execution = _validate_string_list(
            spec["command"], label=f"job {name!r}: invalid command", allow_empty=False
        )

    network = spec.get("network", "none")
    if network not in allowed_networks:
        fail(f"job {name!r}: network {network!r} not allowed")

    group = spec.get("group")
    if group is not None and (not isinstance(group, str) or not NAME_RE.fullmatch(group)):
        fail(f"job {name!r}: invalid group")

    needs = _validate_string_list(spec.get("needs", []), label=f"job {name!r}: invalid needs")
    inputs = _validate_string_list(spec.get("inputs", []), label=f"job {name!r}: invalid inputs")
    secrets = _validate_secrets(spec.get("secrets"), job_name=name)
    artifacts = _validate_artifacts(spec.get("artifacts"), job_name=name)
    env = _validate_env(spec.get("env"), job_name=name)
    tools = _validate_tools(
        spec.get("tools"), job_name=name, package_manager=package_manager
    )
    overlap = sorted(set(env) & set(secrets))
    if overlap:
        fail(f"job {name!r}: environment and secret names overlap: {', '.join(overlap)}")

    normalized = {
        "name": name,
        "group": group,
        "needs": needs,
        "resolved_needs": [],
        "inputs": inputs,
        "resolved_inputs": [],
        "image": image,
        "network": network,
        "env": env,
        "secrets": secrets,
        "artifacts": artifacts,
        "tools": tools,
        mode: execution,
    }
    return normalized



def _collect_groups(jobs: dict[str, dict]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    job_names = set(jobs)
    for name, job in jobs.items():
        group = job.get("group")
        if group is None:
            continue
        if group in job_names:
            fail(f"name {group!r} is used by both a job and a group")
        groups.setdefault(group, []).append(name)
    return groups


def _resolve_refs(
    refs: list[str],
    *,
    jobs: dict[str, dict],
    groups: dict[str, list[str]],
    owner: str,
    field: str,
) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref in jobs:
            candidates = [ref]
        elif ref in groups:
            candidates = groups[ref]
        else:
            fail(f"job {owner!r} {field} unknown job or group {ref!r}")
        for candidate in candidates:
            if candidate == owner:
                fail(f"job {owner!r} depends on itself via {field}")
            if candidate not in seen:
                seen.add(candidate)
                resolved.append(candidate)
    return resolved


def _validate_dag(jobs: dict[str, dict]) -> None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str) -> None:
        mark = state.get(name, 0)
        if mark == 2:
            return
        if mark == 1:
            try:
                start = stack.index(name)
            except ValueError:
                start = 0
            cycle = stack[start:] + [name]
            fail("dependency cycle: " + " -> ".join(cycle))
        state[name] = 1
        stack.append(name)
        for dep in jobs[name]["resolved_needs"]:
            visit(dep)
        stack.pop()
        state[name] = 2

    for name in jobs:
        visit(name)


def _validate_inputs_are_dependencies(jobs: dict[str, dict]) -> None:
    cache: dict[str, set[str]] = {}

    def ancestors(name: str) -> set[str]:
        if name in cache:
            return cache[name]
        result: set[str] = set()
        for dep in jobs[name]["resolved_needs"]:
            result.add(dep)
            result.update(ancestors(dep))
        cache[name] = result
        return result

    for name, job in jobs.items():
        allowed = ancestors(name)
        aliases: dict[str, str] = {}
        for producer in job["resolved_inputs"]:
            if producer not in allowed:
                fail(f"job {name!r}: input {producer!r} is not a dependency")
            if not jobs[producer]["artifacts"]:
                fail(f"job {name!r}: input producer {producer!r} declares no artifacts")
            alias = producer.upper().replace("-", "_")
            previous = aliases.get(alias)
            if previous is not None and previous != producer:
                fail(
                    f"job {name!r}: input environment alias collision between "
                    f"{previous!r} and {producer!r}"
                )
            aliases[alias] = producer


def load_ci_trigger_bytes(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"invalid pipeline JSON: {exc}")
    if not isinstance(data, dict):
        fail("pipeline must be a JSON object")
    if data.get("schema") != 1:
        fail("pipeline.schema must be 1")
    return _validate_trigger(data.get("trigger"), kind="ci")

def load_pipeline_bytes(
    raw: bytes,
    *,
    kind: str,
    branch: str | None = None,
    default_max_parallel: int = 3,
    allowed_networks=("none", "kiln-ci"),
    package_manager: str | None = None,
) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"invalid pipeline JSON: {exc}")
    if not isinstance(data, dict):
        fail("pipeline must be a JSON object")
    if data.get("schema") != 1:
        fail("pipeline.schema must be 1")

    trigger = _validate_trigger(data.get("trigger"), kind=kind)

    requested_parallel = data.get("max_parallel", default_max_parallel)
    if not isinstance(requested_parallel, int) or isinstance(requested_parallel, bool) or requested_parallel < 1:
        fail("pipeline.max_parallel invalid")

    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, dict) or not raw_jobs:
        fail("pipeline.jobs must be a non-empty object")

    allowed_networks = tuple(allowed_networks)
    jobs = {
        name: _normalize_job(
            name, spec, allowed_networks=allowed_networks, package_manager=package_manager
        )
        for name, spec in raw_jobs.items()
    }

    if kind == "ci":
        for name, job in jobs.items():
            if job["secrets"]:
                fail(f"job {name!r}: secrets are release-only")

    groups = _collect_groups(jobs)
    for name, job in jobs.items():
        job["resolved_needs"] = _resolve_refs(
            job["needs"], jobs=jobs, groups=groups, owner=name, field="needs"
        )
        job["resolved_inputs"] = _resolve_refs(
            job["inputs"], jobs=jobs, groups=groups, owner=name, field="inputs"
        )
    _validate_dag(jobs)
    _validate_inputs_are_dependencies(jobs)

    normalized = {
        "schema": 1,
        "max_parallel": requested_parallel,
        "groups": groups,
        "jobs": jobs,
    }
    if trigger is not None:
        normalized["trigger"] = trigger
    return normalized


def matches_branch(pipeline: dict, branch: str) -> bool:
    trigger = pipeline.get("trigger")
    if not isinstance(trigger, dict) or trigger.get("type") != "branch":
        return False
    return any(fnmatch.fnmatchcase(branch, pattern) for pattern in trigger.get("branches", []))
