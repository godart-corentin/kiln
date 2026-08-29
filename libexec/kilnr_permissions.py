#!/usr/bin/env python3
import argparse
import json
import os
import pwd
import stat
import subprocess
from pathlib import Path


MANAGED_BUILD_METADATA = frozenset(
    {"job.json", "status.json", "runtime.json", "pipeline.mk", "status.lock"}
)


class PermissionPolicyError(RuntimeError):
    pass


def _open_directory(name, *, dir_fd=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise PermissionPolicyError(f"unsafe managed directory: {name}: {exc}") from exc


def _open_regular(name, *, dir_fd):
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise PermissionPolicyError(f"unsafe managed file: {name}: {exc}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise PermissionPolicyError(f"unsafe managed file: {name}")
    return fd


def normalize_build_metadata(builds_root):
    root_fd = _open_directory(os.fspath(builds_root))
    try:
        for build_name in os.listdir(root_fd):
            try:
                build_fd = _open_directory(build_name, dir_fd=root_fd)
            except PermissionPolicyError:
                raise
            try:
                entries = set(os.listdir(build_fd))
                for name in sorted(entries & MANAGED_BUILD_METADATA):
                    fd = _open_regular(name, dir_fd=build_fd)
                    try:
                        os.fchmod(fd, 0o640)
                    finally:
                        os.close(fd)
            finally:
                os.close(build_fd)
    finally:
        os.close(root_fd)


def _setfacl_fd(fd, acl):
    try:
        subprocess.run(
            ["setfacl", "-m", acl, f"/proc/self/fd/{fd}"],
            pass_fds=(fd,),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise PermissionPolicyError(f"cannot apply managed ACL: {detail}") from exc


def normalize_repository_refs(repository, kilnr_uid):
    repo_fd = _open_directory(os.fspath(repository))
    opened = [repo_fd]
    try:
        refs_fd = _open_directory("refs", dir_fd=repo_fd)
        opened.append(refs_fd)
        kilnr_fd = _open_directory("kilnr", dir_fd=refs_fd)
        opened.append(kilnr_fd)
        jobs_fd = _open_directory("jobs", dir_fd=kilnr_fd)
        opened.append(jobs_fd)

        directory_acl = (
            f"u::rwx,u:{kilnr_uid}:rwx,g::r-x,m::rwx,o::---,"
            f"d:u::rwx,d:u:{kilnr_uid}:rwx,d:g::r-x,d:m::rwx,d:o::---"
        )
        for fd in (kilnr_fd, jobs_fd):
            _setfacl_fd(fd, directory_acl)

        file_acl = f"u::rw-,u:{kilnr_uid}:rwx,g::r-x,m::rw-,o::---"
        for name in os.listdir(jobs_fd):
            fd = _open_regular(name, dir_fd=jobs_fd)
            try:
                _setfacl_fd(fd, file_acl)
            finally:
                os.close(fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def normalize_configured_repositories(config_root, kilnr_uid):
    for config_path in sorted(Path(config_root).glob("*.json")):
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermissionPolicyError(f"invalid project configuration: {config_path}: {exc}") from exc
        repository = config.get("repository")
        if not isinstance(repository, str) or not os.path.isabs(repository):
            raise PermissionPolicyError(f"invalid repository path: {config_path}")
        normalize_repository_refs(repository, kilnr_uid)


def main(argv=None):
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--normalize-builds")
    group.add_argument("--normalize-repository")
    group.add_argument("--normalize-configured-repositories")
    args = parser.parse_args(argv)
    try:
        if args.normalize_builds:
            normalize_build_metadata(args.normalize_builds)
        else:
            kilnr_uid = pwd.getpwnam("kilnr").pw_uid
            if args.normalize_repository:
                normalize_repository_refs(args.normalize_repository, kilnr_uid)
            else:
                normalize_configured_repositories(
                    args.normalize_configured_repositories, kilnr_uid
                )
    except (KeyError, PermissionPolicyError) as exc:
        parser.exit(1, f"kilnr permissions: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
