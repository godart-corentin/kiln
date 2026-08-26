#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "libexec" / "controller"
ENQUEUE_PATH = ROOT / "libexec" / "enqueue"


def load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


controller = load_script("kiln_controller_test", CONTROLLER_PATH)
enqueue = load_script("kiln_enqueue_test", ENQUEUE_PATH)


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def git_output(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def pipeline(branches, name):
    return {
        "schema": 1,
        "trigger": {
            "type": "branch",
            "branches": branches,
        },
        "jobs": {
            name: {
                "image": "alpine:3.22",
                "network": "none",
                "run": ["true"],
            }
        },
    }


def release_pipeline(name="release"):
    return {
        "schema": 1,
        "jobs": {
            name: {
                "image": "alpine:3.22",
                "network": "none",
                "run": ["true"],
            }
        },
    }


def make_repo(files):
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    work = root / "work"
    bare = root / "repo.git"
    work.mkdir()

    run("git", "init", "-b", "main", cwd=work)
    run("git", "config", "user.email", "kiln-test@example.invalid", cwd=work)
    run("git", "config", "user.name", "Kiln Test", cwd=work)

    for relative, content in files.items():
        path = work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict):
            write_json(path, content)
        else:
            path.write_text(content, encoding="utf-8")

    run("git", "add", ".", cwd=work)
    run("git", "commit", "-m", "test", cwd=work)
    sha = git_output("git", "rev-parse", "HEAD", cwd=work)
    run("git", "clone", "--bare", str(work), str(bare))
    return temp, work, bare, sha


def base_config(repo):
    return {
        "schema": 1,
        "project": "demo",
        "repository": str(repo),
        "runner": {
            "max_parallel": 3,
            "cpus": "0.75",
            "memory": "768m",
            "pids_limit": 256,
            "timeout_seconds": 1800,
            "allowed_networks": ["none", "kiln-ci"],
        },
        "release": {
            "tag_pattern": r"^v[0-9]+\.[0-9]+\.[0-9]+$",
        },
    }


def ci_job(sha, branch):
    return {
        "schema": 1,
        "id": "20260826T000000000000Z-demo-abcdef0-12345678",
        "project": "demo",
        "received_at": "2026-08-26T00:00:00Z",
        "old_sha": sha,
        "new_sha": sha,
        "sha": sha,
        "ref": f"refs/heads/{branch}",
        "type": "ci",
        "event": "push",
        "branch": branch,
        "pin_ref": "refs/kiln/jobs/20260826T000000000000Z-demo-abcdef0-12345678",
    }


def release_job(sha):
    job = ci_job(sha, "main")
    job.update({
        "ref": "refs/tags/v1.2.3",
        "type": "release",
        "event": "tag",
        "tag": "v1.2.3",
    })
    job.pop("branch", None)
    return job


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def test_enqueue_classifies_every_branch_as_ci():
    config = {"release": {"tag_pattern": r"^v[0-9]+\.[0-9]+\.[0-9]+$"}}
    oid = "a" * 40
    old = "b" * 40
    assert_equal(enqueue.classify(config, old, oid, "refs/heads/main"), "ci", "main classification")
    assert_equal(enqueue.classify(config, old, oid, "refs/heads/feature/special"), "ci", "feature classification")


def test_branch_pipeline_selection():
    temp, _work, repo, sha = make_repo({
        ".kiln/pipelines/main.json": pipeline(["main"], "main-check"),
        ".kiln/pipelines/features.json": pipeline(["feature/*"], "feature-check"),
    })
    try:
        cfg = base_config(repo)
        path, selected = controller.select_pipeline(ci_job(sha, "main"), cfg)
        assert_equal(path, ".kiln/pipelines/main.json", "main pipeline")
        assert "main-check" in selected["jobs"]

        path, selected = controller.select_pipeline(ci_job(sha, "feature/login"), cfg)
        assert_equal(path, ".kiln/pipelines/features.json", "feature pipeline")
        assert "feature-check" in selected["jobs"]

        result = controller.select_pipeline(ci_job(sha, "docs"), cfg)
        assert_equal(result, None, "unmatched branch")
    finally:
        temp.cleanup()


def test_selection_uses_exact_job_sha():
    temp, work, repo, old_sha = make_repo({
        ".kiln/pipelines/main.json": pipeline(["main"], "old-job"),
    })
    try:
        write_json(work / ".kiln/pipelines/main.json", pipeline(["main"], "new-job"))
        run("git", "add", ".", cwd=work)
        run("git", "commit", "-m", "new pipeline", cwd=work)
        new_sha = git_output("git", "rev-parse", "HEAD", cwd=work)
        run("git", "push", str(repo), "main", cwd=work)

        cfg = base_config(repo)
        _path, old = controller.select_pipeline(ci_job(old_sha, "main"), cfg)
        _path, new = controller.select_pipeline(ci_job(new_sha, "main"), cfg)
        assert "old-job" in old["jobs"]
        assert "new-job" not in old["jobs"]
        assert "new-job" in new["jobs"]
    finally:
        temp.cleanup()


def test_multiple_branch_pipeline_matches_fail():
    temp, _work, repo, sha = make_repo({
        ".kiln/pipelines/all.json": pipeline(["feature/*"], "all-check"),
        ".kiln/pipelines/special.json": pipeline(["feature/special"], "special-check"),
    })
    try:
        cfg = base_config(repo)
        try:
            controller.select_pipeline(ci_job(sha, "feature/special"), cfg)
        except controller.KilnError as exc:
            if "matches multiple CI pipelines" not in str(exc):
                raise AssertionError(f"unexpected error: {exc}")
        else:
            raise AssertionError("expected multiple pipeline match failure")
    finally:
        temp.cleanup()


def test_release_uses_only_fixed_release_pipeline():
    temp, _work, repo, sha = make_repo({
        ".kiln/pipelines/main.json": pipeline(["main"], "main-check"),
        ".kiln/release.json": release_pipeline("release-job"),
    })
    try:
        cfg = base_config(repo)
        path, selected = controller.select_pipeline(release_job(sha), cfg)
        assert_equal(path, ".kiln/release.json", "release pipeline")
        assert "release-job" in selected["jobs"]
        assert "main-check" not in selected["jobs"]
    finally:
        temp.cleanup()


def test_release_requires_fixed_release_pipeline():
    temp, _work, repo, sha = make_repo({
        ".kiln/pipelines/main.json": pipeline(["main"], "main-check"),
    })
    try:
        cfg = base_config(repo)
        try:
            controller.select_pipeline(release_job(sha), cfg)
        except controller.KilnError as exc:
            if ".kiln/release.json" not in str(exc):
                raise AssertionError(f"unexpected error: {exc}")
        else:
            raise AssertionError("expected missing release pipeline failure")
    finally:
        temp.cleanup()


def test_legacy_pipeline_is_not_used():
    temp, _work, repo, sha = make_repo({
        ".kiln/pipeline.json": pipeline(["main"], "legacy"),
    })
    try:
        cfg = base_config(repo)
        cfg["pipeline"] = ".kiln/pipeline.json"
        cfg["ci"] = {"branches": ["main"]}
        result = controller.select_pipeline(ci_job(sha, "main"), cfg)
        assert_equal(result, None, "legacy pipeline must be ignored")
    finally:
        temp.cleanup()



def test_tools_auto_resolution_uses_package_json_from_exact_sha():
    pipeline_data = pipeline(["main"], "tests")
    pipeline_data["jobs"]["tests"]["tools"] = ["pnpm"]
    temp, work, repo, old_sha = make_repo({
        ".kiln/pipelines/main.json": pipeline_data,
        "package.json": {"packageManager": "pnpm@11.15.1"},
    })
    try:
        package = json.loads((work / "package.json").read_text(encoding="utf-8"))
        package["packageManager"] = "pnpm@11.16.0"
        write_json(work / "package.json", package)
        run("git", "add", ".", cwd=work)
        run("git", "commit", "-m", "bump pnpm", cwd=work)
        new_sha = git_output("git", "rev-parse", "HEAD", cwd=work)
        run("git", "push", str(repo), "main", cwd=work)

        cfg = base_config(repo)
        _path, old = controller.select_pipeline(ci_job(old_sha, "main"), cfg)
        _path, new = controller.select_pipeline(ci_job(new_sha, "main"), cfg)
        assert old["jobs"]["tests"]["tools"] == {"pnpm": "11.15.1"}
        assert new["jobs"]["tests"]["tools"] == {"pnpm": "11.16.0"}
    finally:
        temp.cleanup()

def main():
    tests = [
        test_enqueue_classifies_every_branch_as_ci,
        test_branch_pipeline_selection,
        test_selection_uses_exact_job_sha,
        test_multiple_branch_pipeline_matches_fail,
        test_release_uses_only_fixed_release_pipeline,
        test_release_requires_fixed_release_pipeline,
        test_legacy_pipeline_is_not_used,
        test_tools_auto_resolution_uses_package_json_from_exact_sha,
    ]

    for test in tests:
        test()
        print(f"OK branch pipelines: {test.__name__}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
