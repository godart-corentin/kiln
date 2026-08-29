#!/usr/bin/env python3
import importlib.util
import os
import stat
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "libexec" / "kilnr_permissions.py"


def load_module():
    spec = importlib.util.spec_from_file_location("kilnr_permissions_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_rename_module():
    path = ROOT / "libexec" / "project-rename"
    spec = importlib.util.spec_from_file_location(
        "kilnr_project_rename_permissions_test", path
    )
    if spec is None or spec.loader is None:
        from importlib.machinery import SourceFileLoader

        loader = SourceFileLoader("kilnr_project_rename_permissions_test", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_normalize_build_metadata_repairs_only_managed_files():
    permissions = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        builds = Path(tmp) / "builds"
        build = builds / "20260828-demo-abc"
        build.mkdir(parents=True)
        managed = []
        for name in ("job.json", "status.json", "runtime.json", "pipeline.mk", "status.lock"):
            path = build / name
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o650)
            managed.append(path)
        unrelated = build / "custom-output"
        unrelated.write_text("keep\n", encoding="utf-8")
        unrelated.chmod(0o650)

        permissions.normalize_build_metadata(builds)

        assert [mode(path) for path in managed] == [0o640] * len(managed)
        assert mode(unrelated) == 0o650


def test_normalize_build_metadata_rejects_a_managed_symlink():
    permissions = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        builds = Path(tmp) / "builds"
        build = builds / "20260828-demo-abc"
        build.mkdir(parents=True)
        outside = Path(tmp) / "outside"
        outside.write_text("{}\n", encoding="utf-8")
        outside.chmod(0o650)
        (build / "job.json").symlink_to(outside)

        try:
            permissions.normalize_build_metadata(builds)
        except permissions.PermissionPolicyError as exc:
            assert "unsafe" in str(exc)
        else:
            raise AssertionError("managed metadata symlink was accepted")
        assert mode(outside) == 0o650


def test_repository_ref_acl_normalization_replaces_inherited_other_access():
    if os.name != "posix" or not Path("/proc").exists():
        return
    if subprocess.run(
        ["sh", "-c", "command -v getfacl >/dev/null && command -v setfacl >/dev/null"],
        check=False,
    ).returncode:
        return

    permissions = load_module()
    rename = load_rename_module()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "demo.git"
        jobs = repo / "refs" / "kilnr" / "jobs"
        jobs.mkdir(parents=True)
        loose = jobs / "20260828-demo-abc"
        loose.write_text("deadbeef\n", encoding="ascii")
        subprocess.run(["setfacl", "-m", "d:o::r-x", str(repo / "refs" / "kilnr")], check=True)
        subprocess.run(["setfacl", "-m", "d:o::r-x", str(jobs)], check=True)

        permissions.normalize_repository_refs(repo, os.getuid())

        directory_acl = subprocess.run(
            ["getfacl", "-cpn", str(jobs)], text=True, stdout=subprocess.PIPE, check=True
        ).stdout
        loose_acl = subprocess.run(
            ["getfacl", "-cpn", str(loose)], text=True, stdout=subprocess.PIPE, check=True
        ).stdout
        assert "default:other::---" in directory_acl
        assert "other::---" in loose_acl
        assert mode(jobs) == 0o770
        assert mode(loose) == 0o660
        for path in (repo / "refs" / "kilnr", jobs):
            info = path.stat()
            facts = rename.FileFacts(mode(path), info.st_uid, info.st_gid, rename._read_acl(path))
            rename._validate_kilnr_ref_acl(path, facts, os.getuid())
        info = loose.stat()
        facts = rename.FileFacts(mode(loose), info.st_uid, info.st_gid, rename._read_acl(loose))
        rename._validate_kilnr_loose_ref_acl(loose, facts, os.getuid())


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"OK permissions: {test.__name__}")


if __name__ == "__main__":
    main()
