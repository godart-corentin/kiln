#!/usr/bin/env python3
import errno
import fcntl
import grp
import os
import pwd
import re
import stat
import struct
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import ContextManager, Iterable

PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
PRODUCTION_STATE_ROOT = Path("/var/lib/kilnr")
PRODUCTION_PROJECT_LOCK_ROOT = PRODUCTION_STATE_ROOT / "locks" / "projects"
ACL_ACCESS = "system.posix_acl_access"
ACL_DEFAULT = "system.posix_acl_default"
ACL_UNDEFINED_ID = 0xFFFFFFFF


class ProjectLockBusy(RuntimeError):
    pass


def validate_project_name(name: str) -> str:
    if not isinstance(name, str) or not PROJECT_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid project name: {name!r}")
    return name


def _directory_open_flags(*, search_only=False) -> int:
    flags = os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    if search_only and sys.platform.startswith("linux") and hasattr(os, "O_PATH"):
        return flags | os.O_PATH
    return flags | os.O_RDONLY


def _platform_canonical_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if sys.platform == "darwin" and len(absolute.parts) > 1:
        if absolute.parts[1] == "var":
            return Path("/private/var").joinpath(*absolute.parts[2:])
        if absolute.parts[1] == "tmp":
            return Path("/private/tmp").joinpath(*absolute.parts[2:])
    return absolute


def _open_directory_chain(stack: ExitStack, path: Path, *, search_only=False):
    absolute = _platform_canonical_path(path)
    flags = _directory_open_flags(search_only=search_only)
    root_fd = os.open(os.sep, flags)
    stack.callback(os.close, root_fd)
    opened = [(Path(os.sep), root_fd, os.fstat(root_fd))]
    parent_fd = root_fd
    current = Path(os.sep)
    for component in absolute.parts[1:]:
        fd = os.open(component, flags, dir_fd=parent_fd)
        stack.callback(os.close, fd)
        current /= component
        opened.append((current, fd, os.fstat(fd)))
        parent_fd = fd
    return absolute, opened


def _open_optional_directory(stack: ExitStack, parent_fd: int, name: str):
    try:
        fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    stack.callback(os.close, fd)
    return fd


def _directory_identity(info):
    return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)


def _encode_posix_acl(entries) -> bytes:
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, entry_id)
        for tag, permissions, entry_id in entries
    )


def _decode_posix_acl(value: bytes):
    if len(value) < 4 or (len(value) - 4) % 8:
        raise PermissionError("invalid lock namespace ACL encoding")
    if struct.unpack_from("<I", value)[0] != 2:
        raise PermissionError("invalid lock namespace ACL version")
    entries = []
    for offset in range(4, len(value), 8):
        tag, permissions, entry_id = struct.unpack_from("<HHI", value, offset)
        if tag not in (0x01, 0x02, 0x04, 0x08, 0x10, 0x20):
            raise PermissionError("invalid lock namespace ACL tag")
        if permissions & ~0o7:
            raise PermissionError("invalid lock namespace ACL permissions")
        entries.append((tag, permissions, entry_id))
    return tuple(entries)


def _acl_missing_errnos():
    return {
        getattr(errno, "ENODATA", -1),
        getattr(errno, "ENOATTR", -1),
    }


def _read_acl_xattr(fd: int, name: str):
    try:
        return os.getxattr(fd, name)
    except OSError as exc:
        if exc.errno in _acl_missing_errnos():
            return None
        raise


def _remove_acl_xattr(fd: int, name: str) -> None:
    try:
        os.removexattr(fd, name)
    except OSError as exc:
        if exc.errno not in _acl_missing_errnos():
            raise


def _normalized_acl_ids(values, label):
    try:
        selected = tuple(sorted(set(values)))
    except TypeError as exc:
        raise ValueError(f"invalid {label}") from exc
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value < ACL_UNDEFINED_ID
        for value in selected
    ):
        raise ValueError(f"invalid {label}")
    return selected


def _directory_acl_entries(mode, *, named_users=(), named_groups=()):
    named_users = _normalized_acl_ids(named_users, "named ACL user")
    named_groups = _normalized_acl_ids(named_groups, "named ACL group")
    entries = [(0x01, (mode >> 6) & 0o7, ACL_UNDEFINED_ID)]
    entries.extend((0x02, 0o1, uid) for uid in named_users)
    entries.append((0x04, (mode >> 3) & 0o7, ACL_UNDEFINED_ID))
    entries.extend((0x08, 0o1, gid) for gid in named_groups)
    if named_users or named_groups:
        entries.append((0x10, (mode >> 3) & 0o7, ACL_UNDEFINED_ID))
    entries.append((0x20, mode & 0o7, ACL_UNDEFINED_ID))
    return tuple(entries)


def _verify_no_effective_nonowner_writer(entries) -> None:
    masks = [permissions for tag, permissions, _ in entries if tag == 0x10]
    if len(masks) > 1:
        raise PermissionError("lock namespace ACL has multiple masks")
    mask = masks[0] if masks else 0o7
    for tag, permissions, _entry_id in entries:
        if tag in (0x02, 0x04, 0x08):
            permissions &= mask
        if tag in (0x02, 0x04, 0x08, 0x20) and permissions & 0o2:
            raise PermissionError("lock namespace ACL permits a non-owner writer")


def _apply_directory_policy(
    fd: int,
    uid: int,
    gid: int,
    mode: int,
    *,
    named_users=(),
    named_groups=(),
) -> None:
    entries = _directory_acl_entries(
        mode,
        named_users=named_users,
        named_groups=named_groups,
    )
    info = os.fstat(fd)
    if (info.st_uid, info.st_gid) != (uid, gid):
        os.fchown(fd, uid, gid)
    if sys.platform.startswith("linux"):
        if not all(hasattr(os, name) for name in ("getxattr", "setxattr", "removexattr")):
            raise PermissionError("Linux POSIX ACL operations are unavailable")
        _remove_acl_xattr(fd, ACL_DEFAULT)
        _remove_acl_xattr(fd, ACL_ACCESS)
    if stat.S_IMODE(os.fstat(fd).st_mode) != mode:
        os.fchmod(fd, mode)
    has_named_entries = bool(tuple(named_users) or tuple(named_groups))
    if sys.platform.startswith("linux") and has_named_entries:
        os.setxattr(fd, ACL_ACCESS, _encode_posix_acl(entries))
    if _directory_identity(os.fstat(fd)) != (uid, gid, mode):
        raise PermissionError("cannot apply the required lock namespace policy")
    if sys.platform.startswith("linux"):
        actual_access = _read_acl_xattr(fd, ACL_ACCESS)
        if has_named_entries:
            if actual_access is None:
                raise PermissionError("required lock namespace ACL is missing")
            actual_entries = _decode_posix_acl(actual_access)
            if len(actual_entries) != len(entries) or set(actual_entries) != set(entries):
                raise PermissionError("unexpected lock namespace access ACL")
            _verify_no_effective_nonowner_writer(actual_entries)
        elif actual_access is not None:
            raise PermissionError("unexpected extended lock namespace access ACL")
        if _read_acl_xattr(fd, ACL_DEFAULT) is not None:
            raise PermissionError("unexpected lock namespace default ACL")
    if mode & 0o022:
        raise PermissionError("lock namespace mode permits a non-owner writer")
    os.fsync(fd)


def _validate_existing_directory(fd: int, path: Path, allowed) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"lock namespace component is not a directory: {path}")
    identity = _directory_identity(info)
    if identity not in allowed:
        expected = ", ".join(
            f"{uid}:{gid} {mode:#06o}" for uid, gid, mode in sorted(allowed)
        )
        raise PermissionError(
            f"unexpected lock namespace policy for {path}: "
            f"{identity[0]}:{identity[1]} {identity[2]:#06o}; expected {expected}"
        )


def _create_directory(
    stack: ExitStack,
    parent_fd: int,
    name: str,
    uid: int,
    gid: int,
    mode: int,
    *,
    named_users=(),
    named_groups=(),
):
    os.mkdir(name, mode=mode, dir_fd=parent_fd)
    fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    stack.callback(os.close, fd)
    _apply_directory_policy(
        fd,
        uid,
        gid,
        mode,
        named_users=named_users,
        named_groups=named_groups,
    )
    os.fsync(parent_fd)
    return fd


def _require_same_open_child(parent_fd: int, name: str, expected_fd: int) -> None:
    actual_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    try:
        expected = os.fstat(expected_fd)
        actual = os.fstat(actual_fd)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError(f"lock namespace component changed during provisioning: {name}")
    finally:
        os.close(actual_fd)


def provision_lock_namespace(
    state_root: Path,
    *,
    root_uid: int,
    kilnr_uid: int,
    kilnr_gid: int,
    submit_gid: int,
    projects_mode=0o2750,
    trusted_parent_uid=None,
    state_traverse_group_gids=(),
    lock_traverse_user_uids=(),
) -> None:
    """Safely create or migrate the stable state/locks/projects hierarchy."""

    state_root = Path(os.path.abspath(os.fspath(state_root)))
    state_traverse_group_gids = _normalized_acl_ids(
        state_traverse_group_gids,
        "state traversal group",
    )
    lock_traverse_user_uids = _normalized_acl_ids(
        lock_traverse_user_uids,
        "lock traversal user",
    )
    with ExitStack() as stack:
        _, parent_chain = _open_directory_chain(stack, state_root.parent)
        if trusted_parent_uid is not None:
            for path, _fd, info in parent_chain:
                if info.st_uid != trusted_parent_uid or stat.S_IMODE(info.st_mode) & 0o022:
                    raise PermissionError(f"untrusted lock namespace ancestor: {path}")
        parent_fd = parent_chain[-1][1]

        state_fd = _open_optional_directory(stack, parent_fd, state_root.name)
        lock_fd = None
        projects_fd = None
        if state_fd is not None:
            _validate_existing_directory(
                state_fd,
                state_root,
                {
                    (kilnr_uid, submit_gid, 0o710),
                    (root_uid, submit_gid, 0o710),
                },
            )
            lock_fd = _open_optional_directory(stack, state_fd, "locks")
            if lock_fd is not None:
                _validate_existing_directory(
                    lock_fd,
                    state_root / "locks",
                    {
                        (kilnr_uid, kilnr_gid, 0o750),
                        (root_uid, kilnr_gid, 0o750),
                    },
                )
                projects_fd = _open_optional_directory(stack, lock_fd, "projects")
                if projects_fd is not None:
                    _validate_existing_directory(
                        projects_fd,
                        state_root / "locks" / "projects",
                        {
                            (kilnr_uid, kilnr_gid, 0o750),
                            (root_uid, submit_gid, projects_mode),
                        },
                    )

        if state_fd is None:
            state_fd = _create_directory(
                stack,
                parent_fd,
                state_root.name,
                root_uid,
                submit_gid,
                0o710,
                named_groups=state_traverse_group_gids,
            )
        else:
            _apply_directory_policy(
                state_fd,
                root_uid,
                submit_gid,
                0o710,
                named_groups=state_traverse_group_gids,
            )
            _require_same_open_child(parent_fd, state_root.name, state_fd)

        if lock_fd is None:
            lock_fd = _create_directory(
                stack,
                state_fd,
                "locks",
                root_uid,
                kilnr_gid,
                0o750,
                named_users=lock_traverse_user_uids,
            )
        else:
            _require_same_open_child(state_fd, "locks", lock_fd)
            _apply_directory_policy(
                lock_fd,
                root_uid,
                kilnr_gid,
                0o750,
                named_users=lock_traverse_user_uids,
            )
            _require_same_open_child(state_fd, "locks", lock_fd)

        if projects_fd is None:
            projects_fd = _create_directory(
                stack,
                lock_fd,
                "projects",
                root_uid,
                submit_gid,
                projects_mode,
            )
        else:
            _require_same_open_child(lock_fd, "projects", projects_fd)
            _apply_directory_policy(projects_fd, root_uid, submit_gid, projects_mode)
            _require_same_open_child(lock_fd, "projects", projects_fd)


def provision_production_lock_namespace(state_root=PRODUCTION_STATE_ROOT) -> None:
    try:
        kilnr_user = pwd.getpwnam("kilnr")
        git_user = pwd.getpwnam("git")
        kilnr_group = grp.getgrnam("kilnr")
        submit_group = grp.getgrnam("kilnr-submit")
        readers_group = grp.getgrnam("kilnr-readers")
    except KeyError as exc:
        raise PermissionError(f"required lock namespace identity is missing: {exc}") from exc
    if kilnr_user.pw_gid != kilnr_group.gr_gid:
        raise PermissionError("Linux user 'kilnr' does not use group 'kilnr'")
    provision_lock_namespace(
        Path(state_root),
        root_uid=0,
        kilnr_uid=kilnr_user.pw_uid,
        kilnr_gid=kilnr_group.gr_gid,
        submit_gid=submit_group.gr_gid,
        trusted_parent_uid=0,
        state_traverse_group_gids=(readers_group.gr_gid,),
        lock_traverse_user_uids=(git_user.pw_uid,),
    )


def _validate_production_lock_chain(opened) -> None:
    try:
        kilnr_gid = grp.getgrnam("kilnr").gr_gid
        submit_gid = grp.getgrnam("kilnr-submit").gr_gid
    except KeyError as exc:
        raise PermissionError(f"required lock namespace group is missing: {exc}") from exc
    expected = {
        PRODUCTION_STATE_ROOT: (0, submit_gid, 0o710),
        PRODUCTION_STATE_ROOT / "locks": (0, kilnr_gid, 0o750),
        PRODUCTION_PROJECT_LOCK_ROOT: (0, submit_gid, 0o2750),
    }
    for path, _fd, info in opened:
        if path in expected:
            if _directory_identity(info) != expected[path]:
                raise PermissionError(f"unexpected production lock ancestor policy: {path}")
        elif path in PRODUCTION_STATE_ROOT.parents:
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise PermissionError(f"untrusted production lock ancestor: {path}")


def _open_lock_root(stack: ExitStack, root: Path, *, search_only=False):
    absolute, opened = _open_directory_chain(
        stack,
        root,
        search_only=search_only,
    )
    root_fd = opened[-1][1]
    root_info = os.fstat(root_fd)
    if absolute == PRODUCTION_PROJECT_LOCK_ROOT:
        _validate_production_lock_chain(opened)
    if stat.S_IMODE(root_info.st_mode) & 0o022:
        raise PermissionError(f"project lock namespace is writable by submitters: {root}")
    return root_fd, root_info


def _validate_lock_entry(fd: int, root_info, lock_name: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"project lock entry is not a regular file: {lock_name}")
    if info.st_nlink != 1:
        raise OSError(f"project lock entry has unexpected links: {lock_name}")
    if (info.st_uid, info.st_gid) != (root_info.st_uid, root_info.st_gid):
        raise PermissionError(f"project lock entry has unexpected ownership: {lock_name}")
    if stat.S_IMODE(info.st_mode) != 0o660:
        raise PermissionError(f"project lock entry has unexpected mode: {lock_name}")


def provision_project_locks(root: Path, names: Iterable[str]) -> None:
    """Create root-owned stable lock entries before submitter access is possible."""

    lock_names = sorted({validate_project_name(name) for name in names})
    with ExitStack() as stack:
        root_fd, root_info = _open_lock_root(stack, root)
        changed = False
        for name in lock_names:
            lock_name = f"{name}.lock"
            flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                fd = os.open(
                    lock_name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o660,
                    dir_fd=root_fd,
                )
                created = True
            except FileExistsError:
                fd = os.open(lock_name, flags, dir_fd=root_fd)
                created = False
            stack.callback(os.close, fd)

            entry_changed = created
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError(f"unsafe project lock entry: {lock_name}")
            if (info.st_uid, info.st_gid) != (root_info.st_uid, root_info.st_gid):
                os.fchown(fd, root_info.st_uid, root_info.st_gid)
                entry_changed = True
            if stat.S_IMODE(info.st_mode) != 0o660:
                os.fchmod(fd, 0o660)
                entry_changed = True
            _validate_lock_entry(fd, root_info, lock_name)
            if entry_changed:
                os.fsync(fd)
            changed = changed or entry_changed
        if changed:
            os.fsync(root_fd)


@contextmanager
def project_locks(
    root: Path,
    names: Iterable[str],
    *,
    exclusive: bool,
    blocking: bool = True,
) -> ContextManager[None]:
    lock_names = sorted({validate_project_name(name) for name in names})
    lock_mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        lock_mode |= fcntl.LOCK_NB

    with ExitStack() as stack:
        root_fd, root_info = _open_lock_root(stack, root, search_only=True)
        for name in lock_names:
            lock_name = f"{name}.lock"
            flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
            fd = os.open(lock_name, flags, dir_fd=root_fd)
            stack.callback(os.close, fd)
            _validate_lock_entry(fd, root_info, lock_name)
            try:
                fcntl.flock(fd, lock_mode)
            except BlockingIOError as exc:
                raise ProjectLockBusy(f"project lock is busy: {name}") from exc
        yield


def _main(argv) -> int:
    if len(argv) == 2 and argv[0] == "--provision-production-namespace":
        provision_production_lock_namespace(Path(argv[1]))
        return 0
    raise SystemExit(
        "usage: kilnr_project_lock.py --provision-production-namespace STATE_ROOT"
    )


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
