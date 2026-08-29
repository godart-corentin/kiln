#!/usr/bin/env python3
import contextlib
import errno
import importlib.util
import multiprocessing
import os
import stat
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS_PATH = ROOT / "libexec" / "kilnr_project_lock.py"


def load_python(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


locks = load_python(LOCKS_PATH, "kilnr_project_lock_test")

ACL_ACCESS = "system.posix_acl_access"
ACL_DEFAULT = "system.posix_acl_default"
ACL_UNDEFINED_ID = 0xFFFFFFFF


def encode_posix_acl(entries):
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, entry_id)
        for tag, permissions, entry_id in entries
    )


def decode_posix_acl(value):
    assert len(value) >= 4 and (len(value) - 4) % 8 == 0
    assert struct.unpack_from("<I", value)[0] == 2
    return tuple(
        struct.unpack_from("<HHI", value, offset)
        for offset in range(4, len(value), 8)
    )


def expected_directory_acl(mode, *, named_users=(), named_groups=()):
    entries = [(0x01, (mode >> 6) & 0o7, ACL_UNDEFINED_ID)]
    entries.extend((0x02, 0o1, uid) for uid in sorted(set(named_users)))
    entries.append((0x04, (mode >> 3) & 0o7, ACL_UNDEFINED_ID))
    entries.extend((0x08, 0o1, gid) for gid in sorted(set(named_groups)))
    if named_users or named_groups:
        entries.append((0x10, (mode >> 3) & 0o7, ACL_UNDEFINED_ID))
    entries.append((0x20, mode & 0o7, ACL_UNDEFINED_ID))
    return tuple(entries)


@contextlib.contextmanager
def synthetic_linux_acl_xattrs(initial):
    store = {}

    def inode_for(target):
        info = os.fstat(target) if isinstance(target, int) else Path(target).lstat()
        return info.st_dev, info.st_ino

    for path, values in initial.items():
        device, inode = inode_for(path)
        for name, value in values.items():
            store[(device, inode, name)] = value

    missing_errno = getattr(errno, "ENODATA", getattr(errno, "ENOATTR", 61))

    def normalize_name(name):
        return os.fsdecode(name)

    def getxattr(target, name, *args, **kwargs):
        device, inode = inode_for(target)
        key = (device, inode, normalize_name(name))
        if key not in store:
            raise OSError(missing_errno, "attribute is absent")
        return store[key]

    def setxattr(target, name, value, *args, **kwargs):
        device, inode = inode_for(target)
        store[(device, inode, normalize_name(name))] = value

    def removexattr(target, name, *args, **kwargs):
        device, inode = inode_for(target)
        key = (device, inode, normalize_name(name))
        if key not in store:
            raise OSError(missing_errno, "attribute is absent")
        del store[key]

    def listxattr(target, *args, **kwargs):
        device, inode = inode_for(target)
        return sorted(
            name
            for stored_device, stored_inode, name in store
            if (stored_device, stored_inode) == (device, inode)
        )

    def read(path):
        device, inode = inode_for(path)
        return {
            name: value
            for (stored_device, stored_inode, name), value in store.items()
            if (stored_device, stored_inode) == (device, inode)
        }

    original_platform = locks.sys.platform
    originals = {
        name: getattr(locks.os, name, None)
        for name in ("getxattr", "setxattr", "removexattr", "listxattr")
    }
    locks.sys.platform = "linux"
    locks.os.getxattr = getxattr
    locks.os.setxattr = setxattr
    locks.os.removexattr = removexattr
    locks.os.listxattr = listxattr
    try:
        yield read
    finally:
        locks.sys.platform = original_platform
        for name, original in originals.items():
            if original is None:
                delattr(locks.os, name)
            else:
                setattr(locks.os, name, original)


def provision(root, *names):
    locks.provision_project_locks(Path(root), names)


def child_can_lock(root, name, *, exclusive):
    context = multiprocessing.get_context("fork")
    result = context.Queue()

    def attempt():
        try:
            with locks.project_locks(Path(root), [name], exclusive=exclusive, blocking=False):
                result.put(True)
        except locks.ProjectLockBusy:
            result.put(False)

    process = context.Process(target=attempt)
    process.start()
    process.join(timeout=5)
    assert process.exitcode == 0, f"child did not complete: {process.exitcode}"
    value = result.get(timeout=1)
    result.close()
    result.join_thread()
    return value


def child_holds_lock(root, name, *, exclusive):
    context = multiprocessing.get_context("fork")
    ready = context.Queue()
    release = context.Event()

    def hold():
        with locks.project_locks(Path(root), [name], exclusive=exclusive):
            ready.put(True)
            release.wait(timeout=5)

    process = context.Process(target=hold)
    process.start()
    assert ready.get(timeout=1) is True
    return process, release, ready


def test_project_name_validation_accepts_boundaries():
    accepted = ["a", "z9", "demo_name-2", "a" * 63]
    for name in accepted:
        assert locks.validate_project_name(name) == name


def test_project_name_validation_rejects_invalid_and_path_like_names():
    rejected = [
        "",
        "A",
        "-demo",
        "_demo",
        "demo.",
        "demo/name",
        "../demo",
        "demo\\name",
        "a" * 64,
        None,
        1,
    ]
    for name in rejected:
        try:
            locks.validate_project_name(name)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid project name {name!r}")


def test_project_locks_deduplicate_and_acquire_names_in_sorted_order():
    with tempfile.TemporaryDirectory() as tmp:
        provision(tmp, "zeta", "alpha")
        opened = []
        original_open = locks.os.open

        def record_open(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and Path(path).name.endswith(".lock"):
                opened.append(Path(path).name)
            return original_open(path, *args, **kwargs)

        locks.os.open = record_open
        try:
            with locks.project_locks(Path(tmp), ["zeta", "alpha", "zeta"], exclusive=True):
                pass
        finally:
            locks.os.open = original_open

        assert opened == ["alpha.lock", "zeta.lock"]


def test_project_locks_rejects_a_symlinked_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "target"
        target.mkdir()
        symlinked_root = root / "locks"
        symlinked_root.symlink_to(target, target_is_directory=True)

        try:
            with locks.project_locks(symlinked_root, ["demo"], exclusive=True):
                pass
        except OSError:
            pass
        else:
            raise AssertionError("expected symlinked lock root to be rejected")

        assert not (target / "demo.lock").exists()


def test_project_locks_rejects_a_symlinked_intermediate_ancestor():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        real = base / "real"
        root = real / "state" / "locks" / "projects"
        root.mkdir(parents=True)
        provision(root, "demo")
        alias = base / "alias"
        alias.symlink_to(real, target_is_directory=True)
        aliased_root = alias / "state" / "locks" / "projects"

        try:
            with locks.project_locks(aliased_root, ["demo"], exclusive=True):
                pass
        except OSError:
            pass
        else:
            raise AssertionError("expected intermediate lock ancestor symlink rejection")

        assert (root / "demo.lock").is_file()


def test_project_locks_rejects_a_symlinked_lock_entry():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "target.lock"
        target.write_text("outside lock", encoding="utf-8")
        (root / "demo.lock").symlink_to(target)

        try:
            with locks.project_locks(root, ["demo"], exclusive=True):
                pass
        except OSError:
            pass
        else:
            raise AssertionError("expected symlinked lock entry to be rejected")

        assert target.read_text(encoding="utf-8") == "outside lock"


def test_shared_lock_allows_another_shared_lock():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        provision(root, "demo")
        with locks.project_locks(root, ["demo"], exclusive=False):
            assert child_can_lock(root, "demo", exclusive=False) is True


def test_exclusive_lock_excludes_shared_lock():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        provision(root, "demo")
        with locks.project_locks(root, ["demo"], exclusive=True):
            assert child_can_lock(root, "demo", exclusive=False) is False
        assert child_can_lock(root, "demo", exclusive=False) is True


def test_partial_lock_failure_releases_the_first_sorted_lock():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        provision(root, "alpha", "zeta")
        process, release, ready = child_holds_lock(root, "zeta", exclusive=True)
        try:
            try:
                with locks.project_locks(
                    root,
                    ["zeta", "alpha"],
                    exclusive=True,
                    blocking=False,
                ):
                    pass
            except locks.ProjectLockBusy:
                pass
            else:
                raise AssertionError("expected second sorted lock acquisition to fail")

            assert child_can_lock(root, "alpha", exclusive=True) is True
        finally:
            release.set()
            process.join(timeout=5)
            ready.close()
            ready.join_thread()
            assert process.exitcode == 0, f"lock holder did not complete: {process.exitcode}"


def test_project_lock_releases_after_exception():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        provision(root, "demo")
        try:
            with locks.project_locks(root, ["demo"], exclusive=True):
                raise RuntimeError("abort lifecycle operation")
        except RuntimeError:
            pass
        assert child_can_lock(root, "demo", exclusive=False) is True


def test_provisioned_lock_file_is_group_writable_and_inherits_lock_root_group():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o2750)
        previous_umask = os.umask(0o077)
        try:
            provision(root, "demo")
        finally:
            os.umask(previous_umask)

        lock_stat = (root / "demo.lock").stat()
        assert stat.S_IMODE(lock_stat.st_mode) == 0o660
        assert lock_stat.st_uid == root.stat().st_uid
        assert lock_stat.st_gid == root.stat().st_gid


def test_lock_acquisition_requires_a_preprovisioned_entry():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            with locks.project_locks(root, ["demo"], exclusive=True):
                pass
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("enqueue-style acquisition created a lock entry")
        assert not (root / "demo.lock").exists()


def test_submitter_cannot_unlink_recreate_or_split_a_held_lock_inode():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        provision(root, "demo")
        root.chmod(0o550)
        with locks.project_locks(root, ["demo"], exclusive=True):
            original_inode = (root / "demo.lock").stat().st_ino
            try:
                (root / "demo.lock").unlink()
            except PermissionError:
                pass
            else:
                raise AssertionError("non-writable lock namespace allowed unlink")
            try:
                os.open(root / "replacement", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o660)
            except PermissionError:
                pass
            else:
                raise AssertionError("non-writable lock namespace allowed recreation")
            assert (root / "demo.lock").stat().st_ino == original_inode
            assert child_can_lock(root, "demo", exclusive=False) is False


def test_submitter_cannot_replace_project_lock_namespace_through_ancestors():
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "kilnr"
        lock_parent = state / "locks"
        root = lock_parent / "projects"
        root.mkdir(parents=True)
        provision(root, "demo")

        state.chmod(0o550)
        lock_parent.chmod(0o550)
        root.chmod(0o550)
        with locks.project_locks(root, ["demo"], exclusive=True):
            try:
                root.rename(lock_parent / "projects-replaced")
            except PermissionError:
                pass
            else:
                raise AssertionError("writable lock parent allowed namespace replacement")
            try:
                lock_parent.rename(state / "locks-replaced")
            except PermissionError:
                pass
            else:
                raise AssertionError("writable state root allowed lock-parent replacement")
            assert child_can_lock(root, "demo", exclusive=False) is False


def namespace_identities():
    return {
        "root_uid": os.getuid(),
        "kilnr_uid": os.getuid(),
        "kilnr_gid": os.getgid(),
        "submit_gid": os.getgid(),
        "projects_mode": 0o750 if sys.platform == "darwin" else 0o2750,
    }


def provision_namespace(state):
    provisioner = getattr(locks, "provision_lock_namespace", None)
    assert callable(provisioner), "lock namespace has no no-follow provisioner"
    provisioner(state, **namespace_identities())


def test_lock_namespace_removes_legacy_acls_through_validated_descriptors():
    identities = namespace_identities()
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp).resolve() / "kilnr"
        lock_parent = state / "locks"
        projects = lock_parent / "projects"
        projects.mkdir(parents=True)
        state.chmod(0o710)
        lock_parent.chmod(0o750)
        projects.chmod(identities["projects_mode"])
        initial = {
            path: {
                ACL_ACCESS: b"masked-hostile-named-writer",
                ACL_DEFAULT: b"hostile-inherited-writer",
            }
            for path in (state, lock_parent, projects)
        }

        with synthetic_linux_acl_xattrs(initial) as read_acl:
            locks.provision_lock_namespace(state, **identities)
            for path in (state, lock_parent, projects):
                assert read_acl(path) == {}, f"legacy ACL survived on {path}"


def test_production_namespace_policy_supplies_exact_traversal_identities():
    users = {
        "kilnr": type("User", (), {"pw_uid": 51001, "pw_gid": 51002})(),
        "git": type("User", (), {"pw_uid": 51003, "pw_gid": 51004})(),
    }
    groups = {
        "kilnr": type("Group", (), {"gr_gid": 51002})(),
        "kilnr-submit": type("Group", (), {"gr_gid": 51005})(),
        "kilnr-readers": type("Group", (), {"gr_gid": 51006})(),
    }
    captured = {}
    original_getpwnam = locks.pwd.getpwnam
    original_getgrnam = locks.grp.getgrnam
    original_provision = locks.provision_lock_namespace
    locks.pwd.getpwnam = lambda name: users[name]
    locks.grp.getgrnam = lambda name: groups[name]

    def capture(path, **kwargs):
        captured["path"] = Path(path)
        captured.update(kwargs)

    locks.provision_lock_namespace = capture
    try:
        locks.provision_production_lock_namespace(Path("/fixture/state"))
    finally:
        locks.pwd.getpwnam = original_getpwnam
        locks.grp.getgrnam = original_getgrnam
        locks.provision_lock_namespace = original_provision

    assert captured["state_traverse_group_gids"] == (51006,)
    assert captured["lock_traverse_user_uids"] == (51003,)


def linux_acl_unavailable(exc):
    return exc.errno in {
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOSYS", -1),
    }


def assert_no_acl(path, name):
    try:
        os.getxattr(path, name)
    except OSError as exc:
        assert exc.errno in {
            getattr(errno, "ENODATA", -1),
            getattr(errno, "ENOATTR", -1),
        }, exc
    else:
        raise AssertionError(f"unexpected {name} on {path}")


def assert_no_effective_nonowner_writer(entries):
    masks = [permissions for tag, permissions, _ in entries if tag == 0x10]
    mask = masks[0] if masks else 0o7
    for tag, permissions, _entry_id in entries:
        effective = permissions & mask if tag in (0x02, 0x04, 0x08) else permissions
        if tag in (0x02, 0x04, 0x08, 0x20):
            assert not effective & 0o2, entries


def test_linux_legacy_masked_acl_writers_are_removed_without_target_mutation():
    if (
        not sys.platform.startswith("linux")
        or not all(hasattr(os, name) for name in ("getxattr", "setxattr", "removexattr"))
    ):
        return

    if os.geteuid() == 0:
        root_uid = 0
        kilnr_uid = 52001
        kilnr_gid = 52002
        submit_gid = 52003
    else:
        root_uid = os.getuid()
        kilnr_uid = os.getuid()
        kilnr_gid = os.getgid()
        submit_gid = os.getgid()
    git_uid = 52004 if os.getuid() != 52004 else 62004
    readers_gid = 52005 if os.getgid() != 52005 else 62005
    hostile_uid = 52006 if os.getuid() != 52006 else 62006
    hostile_gid = 52007 if os.getgid() != 52007 else 62007
    policies = (
        ("state", 0o710, submit_gid),
        ("locks", 0o750, kilnr_gid),
        ("projects", 0o750, kilnr_gid),
    )
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        base.chmod(0o755)
        state = base / "kilnr"
        lock_parent = state / "locks"
        projects = lock_parent / "projects"
        projects.mkdir(parents=True)
        paths = {"state": state, "locks": lock_parent, "projects": projects}
        for name, mode, gid in policies:
            path = paths[name]
            if os.geteuid() == 0:
                os.chown(path, kilnr_uid, gid)
            os.chmod(path, mode)
            mask = (mode >> 3) & 0o7
            hostile = encode_posix_acl(
                (
                    (0x01, 0o7, ACL_UNDEFINED_ID),
                    (0x02, 0o7, kilnr_uid),
                    (0x02, 0o7, hostile_uid),
                    (0x04, mask, ACL_UNDEFINED_ID),
                    (0x08, 0o7, hostile_gid),
                    (0x10, mask, ACL_UNDEFINED_ID),
                    (0x20, 0o0, ACL_UNDEFINED_ID),
                )
            )
            hostile_default = encode_posix_acl(
                (
                    (0x01, 0o7, ACL_UNDEFINED_ID),
                    (0x02, 0o7, kilnr_uid),
                    (0x02, 0o7, hostile_uid),
                    (0x04, mask, ACL_UNDEFINED_ID),
                    (0x08, 0o7, hostile_gid),
                    (0x10, 0o7, ACL_UNDEFINED_ID),
                    (0x20, 0o0, ACL_UNDEFINED_ID),
                )
            )
            try:
                os.setxattr(path, ACL_ACCESS, hostile)
                os.setxattr(path, ACL_DEFAULT, hostile_default)
            except OSError as exc:
                if linux_acl_unavailable(exc):
                    return
                raise

        external = base / "external.lock"
        external.write_bytes(b"outside target must not change")
        external.chmod(0o640)
        (projects / "demo.lock").symlink_to(external)
        external_before = (
            external.read_bytes(),
            external.stat().st_ino,
            external.stat().st_uid,
            external.stat().st_gid,
            stat.S_IMODE(external.stat().st_mode),
        )

        try:
            locks.provision_lock_namespace(
                state,
                root_uid=root_uid,
                kilnr_uid=kilnr_uid,
                kilnr_gid=kilnr_gid,
                submit_gid=submit_gid,
                state_traverse_group_gids=(readers_gid,),
                lock_traverse_user_uids=(git_uid,),
            )
        except TypeError as exc:
            raise AssertionError(
                "namespace provisioner cannot establish exact descriptor-safe ACLs"
            ) from exc

        expected = {
            state: expected_directory_acl(
                0o710,
                named_groups=(readers_gid,),
            ),
            lock_parent: expected_directory_acl(
                0o750,
                named_users=(git_uid,),
            ),
        }
        for path, entries in expected.items():
            actual = decode_posix_acl(os.getxattr(path, ACL_ACCESS))
            assert set(actual) == set(entries), path
            assert_no_effective_nonowner_writer(actual)
            assert_no_acl(path, ACL_DEFAULT)
        assert_no_acl(projects, ACL_ACCESS)
        assert_no_acl(projects, ACL_DEFAULT)
        assert (state.stat().st_uid, state.stat().st_gid) == (root_uid, submit_gid)
        assert (lock_parent.stat().st_uid, lock_parent.stat().st_gid) == (
            root_uid,
            kilnr_gid,
        )
        assert (projects.stat().st_uid, projects.stat().st_gid) == (
            root_uid,
            submit_gid,
        )
        assert stat.S_IMODE(state.stat().st_mode) == 0o710
        assert stat.S_IMODE(lock_parent.stat().st_mode) == 0o750
        assert stat.S_IMODE(projects.stat().st_mode) == 0o2750

        try:
            locks.provision_project_locks(projects, ["demo"])
        except OSError:
            pass
        else:
            raise AssertionError("symlinked lock entry was accepted")
        external_after = (
            external.read_bytes(),
            external.stat().st_ino,
            external.stat().st_uid,
            external.stat().st_gid,
            stat.S_IMODE(external.stat().st_mode),
        )
        assert external_after == external_before


def test_linux_nonowner_submitter_traverses_execute_only_lock_ancestors():
    if (
        not sys.platform.startswith("linux")
        or not hasattr(os, "O_PATH")
    ):
        return

    root_run = os.geteuid() == 0
    submit_uid = 53001 if root_run else os.getuid()
    submit_gid = 53002 if root_run else os.getgid()
    kilnr_gid = 53003 if root_run else os.getgid()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        base.chmod(0o755)
        state = base / "kilnr"
        lock_parent = state / "locks"
        projects = lock_parent / "projects"
        projects.mkdir(parents=True)
        lock_file = projects / "demo.lock"
        lock_file.write_bytes(b"")
        if root_run:
            os.chown(state, 0, submit_gid)
            os.chmod(state, 0o710)
            os.chown(lock_parent, 0, kilnr_gid)
            os.chmod(lock_parent, 0o750)
            try:
                os.setxattr(
                    lock_parent,
                    ACL_ACCESS,
                    encode_posix_acl(
                        expected_directory_acl(
                            0o750,
                            named_users=(submit_uid,),
                        )
                    ),
                )
            except OSError as exc:
                if linux_acl_unavailable(exc):
                    return
                raise
            os.chown(projects, 0, submit_gid)
            os.chmod(projects, 0o2750)
        else:
            state.chmod(0o100)
            lock_parent.chmod(0o100)
            projects.chmod(0o500)
        if root_run:
            os.chown(lock_file, 0, submit_gid)
            os.chmod(lock_file, 0o660)
        else:
            os.chmod(lock_file, 0o660)

        context = multiprocessing.get_context("fork")
        receive, send = context.Pipe(duplex=False)

        def acquire_as_submitter():
            try:
                if root_run:
                    os.setgroups([submit_gid])
                    os.setgid(submit_gid)
                    os.setuid(submit_uid)
                with locks.project_locks(projects, ["demo"], exclusive=False):
                    send.send(("ok", None, None))
            except BaseException as exc:
                send.send(("error", type(exc).__name__, getattr(exc, "errno", None)))
            finally:
                send.close()

        process = context.Process(target=acquire_as_submitter)
        process.start()
        send.close()
        assert receive.poll(5), "submitter lock attempt did not report"
        outcome = receive.recv()
        process.join(timeout=5)
        receive.close()
        assert process.exitcode == 0, process.exitcode
        assert outcome == ("ok", None, None), outcome


def test_lock_namespace_provisioning_supports_clean_install_and_legacy_update():
    for legacy in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp).resolve() / "kilnr"
            if legacy:
                lock_parent = state / "locks"
                lock_parent.mkdir(parents=True)
                state.chmod(0o710)
                lock_parent.chmod(0o750)

            provision_namespace(state)

            lock_parent = state / "locks"
            project_root = lock_parent / "projects"
            assert stat.S_IMODE(state.stat().st_mode) == 0o710
            assert stat.S_IMODE(lock_parent.stat().st_mode) == 0o750
            assert stat.S_IMODE(project_root.stat().st_mode) == namespace_identities()[
                "projects_mode"
            ]
            assert (state.stat().st_uid, state.stat().st_gid) == (
                os.getuid(),
                os.getgid(),
            )
            assert (lock_parent.stat().st_uid, lock_parent.stat().st_gid) == (
                os.getuid(),
                os.getgid(),
            )
            assert (project_root.stat().st_uid, project_root.stat().st_gid) == (
                os.getuid(),
                os.getgid(),
            )

            provision(project_root, "demo")
            assert stat.S_IMODE((project_root / "demo.lock").stat().st_mode) == 0o660


def test_lock_namespace_provisioning_rejects_symlink_ancestors_without_mutation():
    scenarios = ("state", "locks", "projects", "dangling-projects")
    for scenario in scenarios:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            state = base / "kilnr"
            target = base / "outside"
            target.mkdir(mode=0o711)
            sentinel = target / "sentinel"
            sentinel.write_bytes(b"outside must not change")
            target_before = (
                stat.S_IMODE(target.stat().st_mode),
                target.stat().st_uid,
                target.stat().st_gid,
                sentinel.read_bytes(),
                tuple(sorted(entry.name for entry in target.iterdir())),
            )

            if scenario == "state":
                state.symlink_to(target, target_is_directory=True)
            else:
                state.mkdir(mode=0o710)
                if scenario == "locks":
                    (state / "locks").symlink_to(target, target_is_directory=True)
                else:
                    lock_parent = state / "locks"
                    lock_parent.mkdir(mode=0o750)
                    destination = (
                        target
                        if scenario == "projects"
                        else base / "missing-target"
                    )
                    (lock_parent / "projects").symlink_to(
                        destination,
                        target_is_directory=True,
                    )

            state_mode_before = (
                stat.S_IMODE(state.stat().st_mode)
                if not state.is_symlink()
                else None
            )
            lock_parent = state / "locks"
            lock_mode_before = (
                stat.S_IMODE(lock_parent.stat().st_mode)
                if lock_parent.exists() and not lock_parent.is_symlink()
                else None
            )
            try:
                provision_namespace(state)
            except OSError:
                pass
            else:
                raise AssertionError(f"expected {scenario} symlink rejection")

            target_after = (
                stat.S_IMODE(target.stat().st_mode),
                target.stat().st_uid,
                target.stat().st_gid,
                sentinel.read_bytes(),
                tuple(sorted(entry.name for entry in target.iterdir())),
            )
            assert target_after == target_before, scenario
            if state_mode_before is not None:
                assert stat.S_IMODE(state.stat().st_mode) == state_mode_before, scenario
            if lock_mode_before is not None:
                assert stat.S_IMODE(lock_parent.stat().st_mode) == lock_mode_before, scenario


def test_controller_home_preserves_the_hardened_state_root_contract():
    service = (ROOT / "systemd" / "kilnr-controller.service").read_text(
        encoding="utf-8"
    )
    assert "Environment=HOME=/var/lib/kilnr/controller-home" in service


def test_install_provisions_controller_lock_before_controller_opens_it():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    controller = (ROOT / "libexec" / "controller").read_text(encoding="utf-8")
    assert 'install -o root -g kilnr -m 0660 /dev/null "$controller_lock"' in install
    assert "os.open(LOCK_FILE, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)" in controller
    assert "os.O_CREAT" not in controller.split("def main():", 1)[1]


def test_install_never_recalculates_stable_lock_ancestor_acl_masks_by_path():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    ancestor_acl_mutations = [
        line.strip()
        for line in install.splitlines()
        if line.strip().startswith("setfacl ")
        and line.strip().endswith((" /var/lib/kilnr", " /var/lib/kilnr/locks"))
    ]
    assert ancestor_acl_mutations == []


def main():
    tests = [
        test_project_name_validation_accepts_boundaries,
        test_project_name_validation_rejects_invalid_and_path_like_names,
        test_project_locks_deduplicate_and_acquire_names_in_sorted_order,
        test_project_locks_rejects_a_symlinked_root,
        test_project_locks_rejects_a_symlinked_intermediate_ancestor,
        test_project_locks_rejects_a_symlinked_lock_entry,
        test_shared_lock_allows_another_shared_lock,
        test_exclusive_lock_excludes_shared_lock,
        test_partial_lock_failure_releases_the_first_sorted_lock,
        test_project_lock_releases_after_exception,
        test_provisioned_lock_file_is_group_writable_and_inherits_lock_root_group,
        test_lock_acquisition_requires_a_preprovisioned_entry,
        test_submitter_cannot_unlink_recreate_or_split_a_held_lock_inode,
        test_submitter_cannot_replace_project_lock_namespace_through_ancestors,
        test_lock_namespace_removes_legacy_acls_through_validated_descriptors,
        test_production_namespace_policy_supplies_exact_traversal_identities,
        test_linux_legacy_masked_acl_writers_are_removed_without_target_mutation,
        test_linux_nonowner_submitter_traverses_execute_only_lock_ancestors,
        test_lock_namespace_provisioning_supports_clean_install_and_legacy_update,
        test_lock_namespace_provisioning_rejects_symlink_ancestors_without_mutation,
        test_controller_home_preserves_the_hardened_state_root_contract,
        test_install_provisions_controller_lock_before_controller_opens_it,
        test_install_never_recalculates_stable_lock_ancestor_acl_masks_by_path,
    ]
    for test in tests:
        test()
        print(f"OK project lock: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
