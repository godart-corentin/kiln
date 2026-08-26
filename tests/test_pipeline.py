#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PATH = ROOT / "libexec" / "pipeline.py"


def load_module():
    spec = importlib.util.spec_from_file_location("kiln_pipeline", PIPELINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load pipeline module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pipeline = load_module()


def base_job(**updates):
    job = {
        "image": "alpine:3.22",
        "run": ["true"],
    }
    job.update(updates)
    return job


def base_pipeline(**updates):
    data = {
        "schema": 1,
        "trigger": {"type": "branch", "branches": ["main"]},
        "jobs": {"tests": base_job()},
    }
    data.update(updates)
    return data


def parse(obj, *, kind="ci", branch="main", default_max_parallel=3):
    return pipeline.load_pipeline_bytes(
        json.dumps(obj).encode(),
        kind=kind,
        branch=branch,
        default_max_parallel=default_max_parallel,
        allowed_networks=("none", "kiln-ci"),
    )


def expect_error(obj, message, *, kind="ci"):
    try:
        parse(obj, kind=kind)
    except pipeline.PipelineError as exc:
        assert message in str(exc), (message, str(exc))
    else:
        raise AssertionError(f"expected PipelineError containing {message!r}")


def test_basic_normalization():
    result = parse(base_pipeline())
    assert result["schema"] == 1
    assert result["max_parallel"] == 3
    assert result["trigger"] == {"type": "branch", "branches": ["main"]}
    assert result["groups"] == {}
    assert list(result["jobs"]) == ["tests"]
    job = result["jobs"]["tests"]
    assert job["name"] == "tests"
    assert job["group"] is None
    assert job["needs"] == []
    assert job["resolved_needs"] == []
    assert job["inputs"] == []
    assert job["resolved_inputs"] == []
    assert job["network"] == "none"
    assert job["env"] == {}
    assert job["secrets"] == []
    assert job["artifacts"] == []
    assert job["run"] == ["true"]


def test_schema_must_be_one():
    expect_error(base_pipeline(schema=2), "pipeline.schema must be 1")


def test_jobs_must_be_non_empty_object():
    expect_error(base_pipeline(jobs={}), "pipeline.jobs must be a non-empty object")
    expect_error(base_pipeline(jobs=[]), "pipeline.jobs must be a non-empty object")


def test_max_parallel_validation():
    assert parse(base_pipeline(max_parallel=2))["max_parallel"] == 2
    expect_error(base_pipeline(max_parallel=0), "pipeline.max_parallel invalid")
    expect_error(base_pipeline(max_parallel="2"), "pipeline.max_parallel invalid")


def test_exactly_one_execution_mode():
    data = base_pipeline(jobs={"x": {"image": "alpine:3.22"}})
    expect_error(data, "must define exactly one of run, script, command")
    data = base_pipeline(jobs={"x": base_job(command=["true"])})
    expect_error(data, "must define exactly one of run, script, command")


def test_run_validation():
    expect_error(base_pipeline(jobs={"x": base_job(run=[])}), "invalid run")
    expect_error(base_pipeline(jobs={"x": base_job(run=[""])}), "invalid run")
    expect_error(base_pipeline(jobs={"x": base_job(run=["ok\x00bad"])}), "invalid run")


def test_script_validation():
    good = base_pipeline(jobs={"x": {"image": "alpine:3.22", "script": "scripts/ci/test.sh"}})
    assert parse(good)["jobs"]["x"]["script"] == "scripts/ci/test.sh"
    expect_error(
        base_pipeline(jobs={"x": {"image": "alpine:3.22", "script": "/tmp/test.sh"}}),
        "invalid script",
    )
    expect_error(
        base_pipeline(jobs={"x": {"image": "alpine:3.22", "script": "../test.sh"}}),
        "invalid script",
    )


def test_command_validation():
    good = base_pipeline(jobs={"x": {"image": "alpine:3.22", "command": ["echo", "ok"]}})
    assert parse(good)["jobs"]["x"]["command"] == ["echo", "ok"]
    expect_error(
        base_pipeline(jobs={"x": {"image": "alpine:3.22", "command": []}}),
        "invalid command",
    )
    expect_error(
        base_pipeline(jobs={"x": {"image": "alpine:3.22", "command": ["ok", 1]}}),
        "invalid command",
    )


def test_network_defaults_and_allowlist():
    assert parse(base_pipeline())["jobs"]["tests"]["network"] == "none"
    data = base_pipeline(jobs={"x": base_job(network="kiln-ci")})
    assert parse(data)["jobs"]["x"]["network"] == "kiln-ci"
    expect_error(base_pipeline(jobs={"x": base_job(network="host")}), "network 'host' not allowed")


def test_reserved_env_is_rejected():
    data = base_pipeline(jobs={"x": base_job(env={"KILN_SHA": "fake"})})
    expect_error(data, "reserved environment variable")


def test_release_has_no_branch_trigger_requirement():
    data = {
        "schema": 1,
        "jobs": {"release": base_job()},
    }
    result = parse(data, kind="release", branch=None)
    assert "trigger" not in result



def test_group_expansion_and_inputs():
    data = base_pipeline(jobs={
        "lint": base_job(group="quality"),
        "tests": base_job(group="quality"),
        "build": base_job(needs=["quality"], inputs=["quality"]),
    })
    result = parse(data)
    assert result["groups"] == {"quality": ["lint", "tests"]}
    assert result["jobs"]["build"]["resolved_needs"] == ["lint", "tests"]
    assert result["jobs"]["build"]["resolved_inputs"] == ["lint", "tests"]


def test_job_group_name_collision_is_rejected():
    data = base_pipeline(jobs={
        "quality": base_job(),
        "lint": base_job(group="quality"),
    })
    expect_error(data, "used by both a job and a group")


def test_unknown_need_is_rejected():
    data = base_pipeline(jobs={"build": base_job(needs=["missing"])})
    expect_error(data, "needs unknown job or group 'missing'")


def test_self_dependency_through_group_is_rejected():
    data = base_pipeline(jobs={
        "tests": base_job(group="quality", needs=["quality"]),
        "lint": base_job(group="quality"),
    })
    expect_error(data, "depends on itself")


def test_direct_cycle_is_rejected():
    data = base_pipeline(jobs={
        "a": base_job(needs=["b"]),
        "b": base_job(needs=["a"]),
    })
    expect_error(data, "dependency cycle")


def test_group_expanded_cycle_is_rejected():
    data = base_pipeline(jobs={
        "a": base_job(group="quality", needs=["build"]),
        "b": base_job(group="quality"),
        "build": base_job(needs=["quality"]),
    })
    expect_error(data, "dependency cycle")


def test_dependency_deduplication_preserves_job_order():
    data = base_pipeline(jobs={
        "lint": base_job(group="quality"),
        "tests": base_job(group="quality"),
        "build": base_job(needs=["tests", "quality", "lint"]),
    })
    result = parse(data)
    assert result["jobs"]["build"]["resolved_needs"] == ["tests", "lint"]


def test_cross_group_job_dependency_is_allowed():
    data = base_pipeline(jobs={
        "lint": base_job(group="quality"),
        "assets": base_job(group="build", needs=["lint"]),
        "package-linux": base_job(group="package", needs=["assets"]),
    })
    result = parse(data)
    assert result["jobs"]["assets"]["resolved_needs"] == ["lint"]
    assert result["jobs"]["package-linux"]["resolved_needs"] == ["assets"]

def main():
    tests = [
        test_basic_normalization,
        test_schema_must_be_one,
        test_jobs_must_be_non_empty_object,
        test_max_parallel_validation,
        test_exactly_one_execution_mode,
        test_run_validation,
        test_script_validation,
        test_command_validation,
        test_network_defaults_and_allowlist,
        test_reserved_env_is_rejected,
        test_release_has_no_branch_trigger_requirement,
        test_group_expansion_and_inputs,
        test_job_group_name_collision_is_rejected,
        test_unknown_need_is_rejected,
        test_self_dependency_through_group_is_rejected,
        test_direct_cycle_is_rejected,
        test_group_expanded_cycle_is_rejected,
        test_dependency_deduplication_preserves_job_order,
        test_cross_group_job_dependency_is_allowed,
    ]
    for test in tests:
        test()
        print(f"OK pipeline: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
