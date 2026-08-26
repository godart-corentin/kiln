#!/usr/bin/env python3
import json
import os
import re
import tempfile
from pathlib import Path

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SECRET_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class SecretError(ValueError):
    pass


def fail(message: str) -> None:
    raise SecretError(message)


def validate_project(project: str) -> str:
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
        fail(f"invalid project name: {project!r}")
    return project


def validate_secret_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not SECRET_RE.fullmatch(name)
        or name.startswith("KILN_")
    ):
        fail(f"invalid secret name: {name!r}")
    return name


def _project_dir(root: Path, project: str) -> Path:
    validate_project(project)
    path = root / project
    if path.is_symlink() or not path.is_dir():
        fail(f"secret directory missing for project {project!r}")
    return path


def _paths(root: Path, project: str, name: str) -> tuple[Path, Path]:
    project_dir = _project_dir(root, project)
    validate_secret_name(name)
    return project_dir / f"{name}.value", project_dir / f"{name}.json"


def _atomic_write(path: Path, data: bytes, *, mode: int, uid=None, gid=None) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        if uid is not None or gid is not None:
            os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if fd != -1:
            os.close(fd)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def store_secret(
    root: Path,
    project: str,
    name: str,
    data: bytes,
    *,
    kind: str,
    scope: str = "release",
    uid=None,
    gid=None,
) -> None:
    if kind not in ("text", "file"):
        fail(f"invalid secret kind: {kind!r}")
    if scope != "release":
        fail(f"unsupported secret scope: {scope!r}")
    if not isinstance(data, (bytes, bytearray)):
        fail("secret value must be bytes")
    data = bytes(data)
    if kind == "text":
        if b"\x00" in data:
            fail("text secret must not contain NUL")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            fail("text secret must be valid UTF-8")
    if not data:
        fail("secret value must not be empty")

    value_path, metadata_path = _paths(root, project, name)
    metadata = json.dumps(
        {"schema": 1, "scope": scope, "kind": kind},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"

    _atomic_write(value_path, data, mode=0o640, uid=uid, gid=gid)
    _atomic_write(metadata_path, metadata, mode=0o640, uid=uid, gid=gid)
    _fsync_dir(value_path.parent)


def load_secret_metadata(root: Path, project: str, name: str) -> dict:
    value_path, metadata_path = _paths(root, project, name)
    if not value_path.is_file() or value_path.is_symlink():
        fail(f"secret {name!r} is not configured for project {project!r}")
    if not metadata_path.is_file() or metadata_path.is_symlink():
        fail(f"secret metadata missing for {name!r}")
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"invalid secret metadata for {name!r}: {exc}")
    if data.get("schema") != 1:
        fail(f"unsupported secret metadata schema for {name!r}")
    if data.get("scope") != "release":
        fail(f"invalid secret scope for {name!r}")
    if data.get("kind") not in ("text", "file"):
        fail(f"invalid secret kind for {name!r}")
    return {
        "schema": 1,
        "scope": data["scope"],
        "kind": data["kind"],
    }


def read_secret_bytes(root: Path, project: str, name: str) -> bytes:
    value_path, _ = _paths(root, project, name)
    load_secret_metadata(root, project, name)
    return value_path.read_bytes()


def list_secrets(root: Path, project: str) -> list[dict]:
    project_dir = _project_dir(root, project)
    result = []
    for metadata_path in sorted(project_dir.glob("*.json")):
        name = metadata_path.stem
        try:
            validate_secret_name(name)
            metadata = load_secret_metadata(root, project, name)
        except SecretError:
            continue
        result.append({
            "name": name,
            "scope": metadata["scope"],
            "kind": metadata["kind"],
        })
    return result


def delete_secret(root: Path, project: str, name: str) -> None:
    value_path, metadata_path = _paths(root, project, name)
    value_path.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)
    _fsync_dir(value_path.parent)


def validate_requested_secrets(
    root: Path,
    project: str,
    names: list[str],
    job_type: str,
) -> dict[str, dict]:
    result = {}
    for name in names:
        metadata = load_secret_metadata(root, project, name)
        if metadata["scope"] == "release" and job_type != "release":
            fail(f"secret {name!r} is release-only")
        result[name] = metadata
    return result
