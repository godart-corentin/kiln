#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PATH = ROOT / "libexec" / "pipeline.py"
EXECUTE_PATH = ROOT / "libexec" / "execute"


def load_python(path, name):
    if path.suffix == ".py":
        spec = importlib.util.spec_from_file_location(name, path)
    else:
        loader = importlib.machinery.SourceFileLoader(name, str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pipeline = load_python(PIPELINE_PATH, "kilnr_pipeline_cache_test")
execute = load_python(EXECUTE_PATH, "kilnr_execute_cache_test")


def parse_job(job, *, kind="ci", package_manager="pnpm@11.15.1"):
    data = {
        "schema": 1,
        "jobs": {"tests": job},
    }
    if kind == "ci":
        data["trigger"] = {"type": "branch", "branches": ["*"]}
    result = pipeline.load_pipeline_bytes(
        json.dumps(data).encode(),
        kind=kind,
        package_manager=package_manager,
        allowed_networks=("none", "kilnr-ci"),
    )
    return result["jobs"]["tests"]


def base_job(**updates):
    job = {
        "image": "node:24-bookworm",
        "tools": ["pnpm"],
        "run": ["pnpm install --frozen-lockfile"],
    }
    job.update(updates)
    return job


def expect_pipeline_error(job, message, *, package_manager="pnpm@11.15.1"):
    try:
        parse_job(job, package_manager=package_manager)
    except pipeline.PipelineError as exc:
        assert message in str(exc), (message, str(exc))
    else:
        raise AssertionError(f"expected PipelineError containing {message!r}")


def test_pnpm_cache_normalizes_to_resolved_tool_version():
    job = parse_job(base_job(cache=["pnpm"]))
    assert job["cache"] == {"pnpm": "11.15.1"}


def test_cache_defaults_to_empty_map():
    job = parse_job(base_job())
    assert job["cache"] == {}


def test_cache_requires_matching_managed_tool():
    expect_pipeline_error(
        {
            "image": "node:24-bookworm",
            "cache": ["pnpm"],
            "run": ["true"],
        },
        "cache 'pnpm' requires managed tool 'pnpm'",
    )


def test_cache_rejects_unknown_names_duplicates_and_invalid_shape():
    expect_pipeline_error(base_job(cache=["yarn"]), "unsupported cache")
    expect_pipeline_error(base_job(cache=["pnpm", "pnpm"]), "duplicate cache")
    expect_pipeline_error(base_job(cache="pnpm"), "invalid cache")


def test_cache_root_is_project_job_type_tool_and_version_scoped():
    with tempfile.TemporaryDirectory() as tmp:
        old_root = execute.CACHE_ROOT
        execute.CACHE_ROOT = Path(tmp)
        try:
            branch_mounts = execute.prepare_cache_mounts(
                {"project": "review_desk", "job_type": "ci"},
                {"cache": {"pnpm": "11.15.1"}},
            )
            release_mounts = execute.prepare_cache_mounts(
                {"project": "review_desk", "job_type": "release"},
                {"cache": {"pnpm": "11.15.1"}},
            )
            other_version = execute.prepare_cache_mounts(
                {"project": "review_desk", "job_type": "ci"},
                {"cache": {"pnpm": "11.16.0"}},
            )
            other_project = execute.prepare_cache_mounts(
                {"project": "other", "job_type": "ci"},
                {"cache": {"pnpm": "11.15.1"}},
            )
        finally:
            execute.CACHE_ROOT = old_root

        assert branch_mounts != release_mounts
        assert branch_mounts != other_version
        assert branch_mounts != other_project
        mount = branch_mounts[0]
        assert "/review_desk/ci/pnpm/11.15.1" in mount
        assert "dst=/run/kilnr/cache/pnpm" in mount
        assert "readonly" not in mount


def test_public_env_configures_pnpm_store_only_when_cache_enabled():
    runtime = {
        "build_id": "build-1",
        "project": "review_desk",
        "sha": "a" * 40,
        "ref": "refs/heads/main",
        "job_type": "ci",
        "branch": "main",
    }
    cached = execute.build_public_env(
        runtime,
        "tests",
        {"env": {}, "cache": {"pnpm": "11.15.1"}},
        {},
    )
    uncached = execute.build_public_env(runtime, "tests", {"env": {}}, {})

    assert cached["PNPM_CONFIG_STORE_DIR"] == "/run/kilnr/cache/pnpm"
    assert "PNPM_CONFIG_STORE_DIR" not in uncached
    assert "npm_config_store_dir" not in cached


def test_install_declares_private_cache_root():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "/var/lib/kilnr/cache" in text
    assert "install -d -o kilnr -g kilnr -m 0700" in text


def main():
    tests = [
        test_pnpm_cache_normalizes_to_resolved_tool_version,
        test_cache_defaults_to_empty_map,
        test_cache_requires_matching_managed_tool,
        test_cache_rejects_unknown_names_duplicates_and_invalid_shape,
        test_cache_root_is_project_job_type_tool_and_version_scoped,
        test_public_env_configures_pnpm_store_only_when_cache_enabled,
        test_install_declares_private_cache_root,
    ]
    for test in tests:
        test()
        print(f"OK cache: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
