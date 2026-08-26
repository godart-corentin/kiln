#!/usr/bin/env python3
import os
import shutil
import stat
from pathlib import Path


class ArtifactError(ValueError):
    pass


def fail(message: str) -> None:
    raise ArtifactError(message)


def _relative(path: Path, workspace: Path) -> Path:
    try:
        return path.relative_to(workspace)
    except ValueError:
        fail(f"artifact escapes workspace: {path}")


def _reject_symlink_components(path: Path, workspace: Path) -> None:
    relative = _relative(path, workspace)
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            fail(f"artifact path contains symlink: {relative}")


def _files_for_match(match: Path, workspace: Path):
    _reject_symlink_components(match, workspace)
    if match.is_symlink():
        fail(f"artifact path is a symlink: {_relative(match, workspace)}")
    if match.is_file():
        yield match
        return
    if not match.is_dir():
        return

    for root, dirs, files in os.walk(match, followlinks=False):
        root_path = Path(root)
        for dirname in list(dirs):
            child = root_path / dirname
            if child.is_symlink():
                fail(f"artifact path contains symlink: {_relative(child, workspace)}")
        for filename in files:
            child = root_path / filename
            _reject_symlink_components(child, workspace)
            if child.is_symlink():
                fail(f"artifact path is a symlink: {_relative(child, workspace)}")
            if child.is_file():
                yield child


def collect_artifacts(workspace: Path, patterns: list[str], dest: Path) -> list[str]:
    workspace = workspace.resolve()
    if not workspace.is_dir():
        fail(f"workspace does not exist: {workspace}")

    selected: dict[str, Path] = {}
    for pattern in patterns:
        matched_for_pattern = 0
        for match in workspace.glob(pattern):
            for source in _files_for_match(match, workspace):
                relative = _relative(source, workspace)
                key = relative.as_posix()
                selected[key] = source
                matched_for_pattern += 1
        if matched_for_pattern == 0:
            fail(f"artifact pattern matched no files: {pattern}")

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(mode=0o750, parents=True)

    for relative, source in sorted(selected.items()):
        target = dest / relative
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        mode = stat.S_IMODE(source.stat().st_mode) & 0o777
        target.chmod(mode)

    return sorted(selected)


def resolve_input_roots(build_dir: Path, producers: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for producer in producers:
        root = build_dir / "artifacts" / producer
        if not root.is_dir():
            fail(f"input artifacts unavailable for producer {producer!r}")
        roots[producer] = root
    return roots
