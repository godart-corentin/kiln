#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "libexec" / "controller"
PIPELINE_PATH = ROOT / "libexec" / "pipeline.py"


def load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


controller = load_script("kiln_controller_dag_test", CONTROLLER_PATH)
pipeline_schema = load_script("kiln_pipeline_dag_test", PIPELINE_PATH)


def job():
    return {
        "schema": 1,
        "id": "20260826T000000000000Z-demo-abcdef0-12345678",
        "project": "demo",
        "received_at": "2026-08-26T00:00:00Z",
        "old_sha": "a" * 40,
        "new_sha": "a" * 40,
        "sha": "a" * 40,
        "ref": "refs/heads/main",
        "type": "ci",
        "event": "push",
        "branch": "main",
        "pin_ref": "refs/kiln/jobs/20260826T000000000000Z-demo-abcdef0-12345678",
    }


def config(max_parallel=3):
    return {
        "runner": {
            "max_parallel": max_parallel,
            "cpus": "0.75",
            "memory": "768m",
            "pids_limit": 256,
            "timeout_seconds": 1800,
            "allowed_networks": ["none", "kiln-ci"],
        }
    }


def normalized_pipeline(requested=4):
    raw = {
        "schema": 1,
        "trigger": {"type": "branch", "branches": ["main"]},
        "max_parallel": requested,
        "jobs": {
            "lint": {
                "group": "quality",
                "image": "alpine:3.22",
                "run": ["echo lint"],
            },
            "tests": {
                "group": "quality",
                "image": "alpine:3.22",
                "run": ["echo tests"],
            },
            "build": {
                "group": "build-group",
                "needs": ["quality"],
                "image": "alpine:3.22",
                "run": ["echo build"],
            },
            "package": {
                "needs": ["build"],
                "image": "alpine:3.22",
                "command": ["true"],
            },
        },
    }
    return pipeline_schema.load_pipeline_bytes(
        json.dumps(raw).encode(),
        kind="ci",
        branch="main",
        default_max_parallel=3,
        allowed_networks=("none", "kiln-ci"),
    )


def test_runtime_uses_jobs_and_caps_parallelism():
    runtime = controller.resolve_pipeline(
        job(), config(max_parallel=3), normalized_pipeline(requested=8), ".kiln/pipelines/ci.json"
    )
    assert "jobs" in runtime
    assert "steps" not in runtime
    assert runtime["groups"] == {"quality": ["lint", "tests"], "build-group": ["build"]}
    assert runtime["jobs"]["build"]["resolved_needs"] == ["lint", "tests"]
    assert runtime["jobs"]["package"]["resolved_needs"] == ["build"]
    assert runtime["max_parallel"] == 3


def test_makefile_uses_resolved_job_dependencies_without_group_targets():
    runtime = controller.resolve_pipeline(
        job(), config(max_parallel=4), normalized_pipeline(requested=4), ".kiln/pipelines/ci.json"
    )
    with tempfile.TemporaryDirectory() as tmp:
        build_dir = Path(tmp)
        controller.write_makefile(build_dir, runtime)
        text = (build_dir / "pipeline.mk").read_text(encoding="utf-8")

    assert "job-build: job-lint job-tests" in text
    assert "job-package: job-build" in text
    assert "job-quality:" not in text
    assert "/usr/local/libexec/kiln/execute" in text
    assert runtime["build_id"] in text



def test_status_contains_resolved_pipeline_jobs():
    runtime = controller.resolve_pipeline(
        job(), config(max_parallel=4), normalized_pipeline(requested=4), ".kiln/pipelines/ci.json"
    )
    with tempfile.TemporaryDirectory() as tmp:
        build_dir = Path(tmp)
        (build_dir / "logs").mkdir()
        status = controller.initial_status(job(), ".kiln/pipelines/ci.json")
        controller.write_json(build_dir / "status.json", status)
        controller.start_pipeline_status(build_dir, status, runtime)
        stored = controller.read_json(build_dir / "status.json")

    assert stored["pipeline_path"] == ".kiln/pipelines/ci.json"
    assert stored["prepare"]["state"] == "success"
    assert stored["pipeline"]["groups"] == {"quality": ["lint", "tests"], "build-group": ["build"]}
    assert stored["pipeline"]["jobs"]["build"]["needs"] == ["quality"]
    assert stored["pipeline"]["jobs"]["build"]["resolved_needs"] == ["lint", "tests"]
    assert stored["pipeline"]["jobs"]["build"]["state"] == "pending"
    assert "run" not in stored["pipeline"]["jobs"]["build"]


def test_finalize_marks_pending_jobs_skipped():
    runtime = controller.resolve_pipeline(
        job(), config(max_parallel=4), normalized_pipeline(requested=4), ".kiln/pipelines/ci.json"
    )
    with tempfile.TemporaryDirectory() as tmp:
        build_dir = Path(tmp)
        (build_dir / "logs").mkdir()
        status = controller.initial_status(job(), ".kiln/pipelines/ci.json")
        controller.write_json(build_dir / "status.json", status)
        controller.start_pipeline_status(build_dir, status, runtime)
        stored = controller.read_json(build_dir / "status.json")
        stored["pipeline"]["jobs"]["lint"]["state"] = "failed"
        stored["pipeline"]["jobs"]["tests"]["state"] = "success"
        controller.write_json(build_dir / "status.json", stored)
        state = controller.finalize(build_dir, make_rc=2)
        final = controller.read_json(build_dir / "status.json")

    assert state == "failed"
    assert final["pipeline"]["jobs"]["build"]["state"] == "skipped"
    assert final["pipeline"]["jobs"]["package"]["state"] == "skipped"

def main():
    tests = [
        test_runtime_uses_jobs_and_caps_parallelism,
        test_makefile_uses_resolved_job_dependencies_without_group_targets,
        test_status_contains_resolved_pipeline_jobs,
        test_finalize_marks_pending_jobs_skipped,
    ]
    for test in tests:
        test()
        print(f"OK controller DAG: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
