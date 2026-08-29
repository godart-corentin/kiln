#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import contextlib
import errno
import io
import json
import multiprocessing
import os
import shutil
import shlex
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENAME_PATH = ROOT / "libexec" / "project-rename"


def load_python(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rename = load_python(RENAME_PATH, "kilnr_project_rename_test")

OLD = "old-app"
NEW = "new_app"
SHA = "b7be08123e3518e46578aa35e713f6190a3ce45a"
OLD_ID = "20260828T010203123456Z-old-app-b7be081-0123abcd"
NEW_ID = "20260828T010203123456Z-new_app-b7be081-0123abcd"
OLD_ID_2 = "20260828T010203123456Z-old-app-b7be081-deadbeef"
NEW_ID_2 = "20260828T010203123456Z-new_app-b7be081-deadbeef"


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def make_roots(root):
    git = root / "git"
    config = root / "projects"
    secrets = root / "secrets"
    state = root / "state"
    locks = state / "locks" / "projects"
    for path in (
        git,
        config,
        secrets,
        state / "queue" / "incoming",
        state / "queue" / "running",
        state / "builds",
        state / "cache",
        locks,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return rename.Roots(git=git, config=config, secrets=secrets, state=state, locks=locks)


def make_project(roots, project=OLD):
    repo = roots.git / f"{project}.git"
    subprocess.run(
        ["/usr/bin/git", "init", "--bare", "--initial-branch=main", str(repo)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["/usr/bin/git", f"--git-dir={repo}", "config", "transfer.hideRefs", "refs/kilnr/"],
        check=True,
    )
    subprocess.run(
        ["/usr/bin/git", f"--git-dir={repo}", "config", "gc.packRefs", "false"],
        check=True,
    )
    object_result = subprocess.run(
        ["/usr/bin/git", f"--git-dir={repo}", "hash-object", "-w", "--stdin"],
        input=b"fixture object",
        stdout=subprocess.PIPE,
        check=True,
    )
    assert object_result.stdout.decode("ascii").strip() == SHA
    os.chmod(repo, 0o750)
    (repo / "refs" / "kilnr" / "jobs").mkdir(parents=True)
    # project-create installs these as 0750, then its named rwx ACL expands the
    # ACL mask represented by the group mode bits.  stat therefore reports 0770.
    os.chmod(repo / "refs" / "kilnr", 0o770)
    os.chmod(repo / "refs" / "kilnr" / "jobs", 0o770)
    hook = repo / "hooks" / "post-receive"
    hook.symlink_to(ROOT / "libexec" / "git-hooks" / "post-receive")
    webhook = roots.secrets / f"{project}.discord-webhook"
    webhook.write_text("https://discord.invalid/token\n", encoding="utf-8")
    os.chmod(webhook, 0o640)
    secret_dir = roots.secrets / project
    secret_dir.mkdir(mode=0o750)
    (secret_dir / "TOKEN.value").write_bytes(b"old-app\x00\xffprivate")
    write_json(secret_dir / "TOKEN.json", {"schema": 1, "scope": "release", "kind": "file"})
    os.chmod(secret_dir / "TOKEN.value", 0o640)
    os.chmod(secret_dir / "TOKEN.json", 0o640)
    cache = roots.state / "cache" / project
    previous_umask = os.umask(0o027)
    try:
        # execute supplies 0700 only for the deepest version directory.  This
        # missing project parent is created with 0777 & UMask=0027 == 0750.
        cache.mkdir()
    finally:
        os.umask(previous_umask)
    config = {
        "schema": 1,
        "project": project,
        "repository": str(repo),
        "release": {"tag_pattern": r"^v[0-9]+$"},
        "runner": {
            "max_parallel": 2,
            "cpus": "1.0",
            "memory": "1G",
            "pids_limit": 128,
            "timeout_seconds": 600,
            "allowed_networks": ["none"],
        },
        "discord": {"webhook_file": str(webhook)},
    }
    config_path = roots.config / f"{project}.json"
    write_json(config_path, config)
    os.chmod(config_path, 0o644)
    return repo


def job_data(project=OLD, build_id=OLD_ID):
    return {
        "schema": 1,
        "id": build_id,
        "project": project,
        "received_at": "2026-08-28T01:02:03.123456Z",
        "old_sha": "0" * 40,
        "new_sha": SHA,
        "sha": SHA,
        "ref": "refs/heads/feature/old-app-history",
        "type": "ci",
        "event": "push",
        "pin_ref": f"refs/kilnr/jobs/{build_id}",
    }


def runtime_data(project=OLD, build_id=OLD_ID):
    return {
        "schema": 1,
        "build_id": build_id,
        "project": project,
        "sha": SHA,
        "ref": "refs/heads/feature/old-app-history",
        "job_type": "ci",
        "pipeline": ".kilnr/pipelines/ci.json",
        "max_parallel": 1,
        "runner": {
            "cpus": "1.0",
            "memory": "1G",
            "pids_limit": 128,
            "timeout_seconds": 600,
        },
        "groups": {},
        "jobs": {"test": {"resolved_needs": []}},
    }


def status_data(project=OLD, build_id=OLD_ID):
    return {
        "schema": 1,
        "build_id": build_id,
        "job_id": build_id,
        "project": project,
        "sha": SHA,
        "ref": "refs/heads/feature/old-app-history",
        "type": "ci",
        "event": "push",
        "received_at": "2026-08-28T01:02:03.123456Z",
        "state": "success",
        "prepare": {"state": "success", "log": "logs/prepare.log"},
        "pipeline": {"jobs": {}},
    }


def make_build(roots, project=OLD, build_id=OLD_ID, *, complete=True):
    build = roots.state / "builds" / build_id
    build.mkdir(mode=0o750)
    write_json(build / "job.json", job_data(project, build_id))
    os.chmod(build / "job.json", 0o640)
    if complete:
        write_json(build / "runtime.json", runtime_data(project, build_id))
        write_json(build / "status.json", status_data(project, build_id))
        (build / "pipeline.mk").write_text(
            ".DELETE_ON_ERROR:\n"
            ".PHONY: all job-test\n\n"
            "all: job-test\n\n"
            "job-test:\n"
            f"\t@/usr/local/libexec/kilnr/execute {build_id} test\n",
            encoding="utf-8",
        )
        for path in (build / "runtime.json", build / "status.json", build / "pipeline.mk"):
            os.chmod(path, 0o640)
    for name in ("src", "work", "logs", "artifacts", "commands"):
        (build / name).mkdir(mode=0o750)
    return build


def make_terminal_prepare_failure(
    roots,
    *,
    build_id=OLD_ID_2,
    selected_pipeline=False,
):
    build = make_build(roots, build_id=build_id, complete=False)
    pipeline_path = ".kilnr/pipelines/ci.json" if selected_pipeline else None
    status = {
        "schema": 1,
        "build_id": build_id,
        "job_id": build_id,
        "project": OLD,
        "sha": SHA,
        "ref": "refs/heads/feature/old-app-history",
        "type": "ci",
        "event": "push",
        "pipeline_path": pipeline_path,
        "pipeline": None,
        "prepare": {
            "state": "failed",
            "exit_code": None,
            "started_at": "2026-08-28T01:02:03.123456Z",
            "finished_at": "2026-08-28T01:02:04.123456Z",
            "duration_seconds": 1.0,
            "log": "logs/prepare.log",
            "error": "selection failed" if not selected_pipeline else "extraction failed",
        },
        "state": "failed",
        "received_at": "2026-08-28T01:02:03.123456Z",
        "started_at": "2026-08-28T01:02:03.123456Z",
        "finished_at": "2026-08-28T01:02:04.123456Z",
        "duration_seconds": 1.0,
    }
    write_json(build / "status.json", status)
    os.chmod(build / "status.json", 0o640)
    (build / "logs" / "prepare.log").write_text(
        "KILNR ERROR: old-app remains historical\n", encoding="utf-8"
    )
    return build


def make_fixture(root):
    roots = make_roots(root)
    repo = make_project(roots)
    build = make_build(roots)
    original_bytes = b"\x00old-app\xff" * 8
    (build / "artifacts" / "old-app.bin").write_bytes(original_bytes)
    (build / "src" / "identity.txt").write_text(OLD_ID, encoding="utf-8")
    (build / "work" / "old-app.bin").write_bytes(original_bytes)
    (build / "logs" / "old-app.log").write_bytes(original_bytes)
    cache_payload = roots.state / "cache" / OLD / "opaque.bin"
    cache_payload.write_bytes(original_bytes)
    (repo / "objects" / "old-app-object").write_bytes(original_bytes)
    loose_ref = repo / "refs" / "kilnr" / "jobs" / OLD_ID
    loose_ref.write_text(SHA + "\n", encoding="ascii")
    os.chmod(loose_ref, 0o640)
    return roots, repo, build, original_bytes


def expect_rename_error(action, message):
    try:
        action()
    except rename.RenameError as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError(f"expected RenameError containing {message!r}")


def prepared_bytes(prepared, destination):
    for item in prepared.files:
        if item.write.destination == destination:
            return item.temporary.read_bytes(), item
    raise AssertionError(f"missing prepared metadata for {destination}")


def staged_files(root):
    return [path for path in root.rglob("*") if path.name.endswith(".rename-tmp")]


def snapshot_tree(root):
    records = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root))
        info = path.lstat()
        common = (relative, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
        if stat.S_ISREG(info.st_mode):
            records.append(("file",) + common + (path.read_bytes(),))
        elif stat.S_ISDIR(info.st_mode):
            records.append(("directory",) + common)
        elif stat.S_ISLNK(info.st_mode):
            records.append(("symlink",) + common + (os.readlink(path),))
        else:
            records.append(("special", stat.S_IFMT(info.st_mode)) + common)
    return tuple(records)


def snapshot_tree_with_real_acls(root):
    acl_records = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        acl_records.append((str(path.relative_to(root)), rename._read_acl(path)))
    return snapshot_tree(root), tuple(acl_records)


def ref_value(repository, ref):
    result = subprocess.run(
        ["/usr/bin/git", f"--git-dir={repository}", "show-ref", "--verify", "--hash", ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def prepare_fixture(root):
    roots, repo, build, original_bytes = make_fixture(root)
    inventory = rename.inventory_rename(roots, OLD, NEW)
    return roots, repo, build, original_bytes, rename.prepare_rename(inventory)


def test_inventory_maps_hyphenated_build_identity_without_mutating_state():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, build, original_bytes = make_fixture(Path(tmp))
        before = sorted(str(path.relative_to(Path(tmp))) for path in Path(tmp).rglob("*"))

        inventory = rename.inventory_rename(roots, OLD, NEW)

        after = sorted(str(path.relative_to(Path(tmp))) for path in Path(tmp).rglob("*"))
        assert inventory.old == OLD
        assert inventory.new == NEW
        assert inventory.build_ids == {OLD_ID: NEW_ID}
        assert inventory.pin_refs == {
            f"refs/kilnr/jobs/{OLD_ID}": f"refs/kilnr/jobs/{NEW_ID}"
        }
        assert inventory.repository.source == repo
        assert inventory.repository.destination == roots.git / f"{NEW}.git"
        assert before == after
        assert (build / "artifacts" / "old-app.bin").read_bytes() == original_bytes


def test_inventory_ignores_unrelated_builds_and_active_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, _, _ = make_fixture(Path(tmp))
        unrelated_id = "not-a-kilnr-build-id"
        make_build(roots, "another", unrelated_id, complete=False)
        unrelated_job_id = "20260828T010203123456Z-another-abcdef0-0123abcd"
        write_json(
            roots.state / "queue" / "incoming" / f"{unrelated_job_id}.json",
            job_data("another", unrelated_job_id),
        )

        inventory = rename.inventory_rename(roots, OLD, NEW)

        assert inventory.build_ids == {OLD_ID: NEW_ID}


def test_inventory_rejects_source_jobs_in_both_active_queues():
    for queue in ("incoming", "running"):
        with tempfile.TemporaryDirectory() as tmp:
            roots, _, _, _ = make_fixture(Path(tmp))
            write_json(
                roots.state / "queue" / queue / f"{OLD_ID}.json",
                job_data(),
            )
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                f"active {queue} job",
            )


def test_inventory_rejects_invalid_names_before_path_construction():
    with tempfile.TemporaryDirectory() as tmp:
        roots = make_roots(Path(tmp))
        for old, new in (("../old", NEW), (OLD, "New"), (OLD, OLD)):
            expect_rename_error(
                lambda old=old, new=new: rename.inventory_rename(roots, old, new),
                "project name" if old != new else "different",
            )


def test_inventory_rejects_every_fixed_destination_collision():
    collision_paths = (
        lambda roots: roots.git / f"{NEW}.git",
        lambda roots: roots.config / f"{NEW}.json",
        lambda roots: roots.secrets / f"{NEW}.discord-webhook",
        lambda roots: roots.secrets / NEW,
        lambda roots: roots.state / "cache" / NEW,
        lambda roots: roots.state / "builds" / NEW_ID,
    )
    for choose in collision_paths:
        with tempfile.TemporaryDirectory() as tmp:
            roots, _, _, _ = make_fixture(Path(tmp))
            collision = choose(roots)
            if collision == roots.state / "builds" / NEW_ID:
                make_build(roots, project=NEW, build_id=NEW_ID, complete=False)
            elif collision.suffix in (".json", ".discord-webhook"):
                collision.write_text("occupied", encoding="utf-8")
            else:
                collision.mkdir()
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                str(collision),
            )


def test_inventory_rejects_a_cross_filesystem_source_move():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        original_lstat = rename._lstat

        def different_device(path, description):
            info = original_lstat(path, description)
            if Path(path) != repo:
                return info
            fields = list(info)
            fields[2] += 1
            return os.stat_result(fields)

        rename._lstat = different_device
        try:
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "same filesystem",
            )
        finally:
            rename._lstat = original_lstat


def test_inventory_rejects_dangling_destination_symlinks():
    selectors = (
        lambda roots: roots.config / f"{NEW}.json",
        lambda roots: roots.state / "builds" / NEW_ID,
    )
    for choose in selectors:
        with tempfile.TemporaryDirectory() as tmp:
            roots, _, _, _ = make_fixture(Path(tmp))
            destination = choose(roots)
            destination.symlink_to(destination.parent / "missing-target")
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                str(destination),
            )


def test_inventory_rejects_a_destination_pin_ref_collision():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        (repo / "refs" / "kilnr" / "jobs" / NEW_ID).write_text(SHA + "\n", encoding="ascii")
        expect_rename_error(
            lambda: rename.inventory_rename(roots, OLD, NEW),
            "destination pin ref exists",
        )


def test_inventory_maps_a_source_pin_ref_stored_in_packed_refs():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        (repo / "refs" / "kilnr" / "jobs" / OLD_ID).unlink()
        (repo / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{SHA} refs/kilnr/jobs/{OLD_ID}\n",
            encoding="ascii",
        )

        inventory = rename.inventory_rename(roots, OLD, NEW)

        assert inventory.pin_refs == {
            f"refs/kilnr/jobs/{OLD_ID}": f"refs/kilnr/jobs/{NEW_ID}"
        }


def test_inventory_maps_packed_refs_when_the_loose_namespace_is_absent():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        jobs = repo / "refs" / "kilnr" / "jobs"
        (jobs / OLD_ID).unlink()
        jobs.rmdir()
        (repo / "packed-refs").write_text(
            f"{SHA} refs/kilnr/jobs/{OLD_ID}\n", encoding="ascii"
        )

        inventory = rename.inventory_rename(roots, OLD, NEW)

        assert inventory.pin_refs == {
            f"refs/kilnr/jobs/{OLD_ID}": f"refs/kilnr/jobs/{NEW_ID}"
        }


def test_inventory_rejects_a_packed_destination_when_loose_namespace_is_absent():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        jobs = repo / "refs" / "kilnr" / "jobs"
        (jobs / OLD_ID).unlink()
        jobs.rmdir()
        (repo / "packed-refs").write_text(
            f"{SHA} refs/kilnr/jobs/{OLD_ID}\n"
            f"{SHA} refs/kilnr/jobs/{NEW_ID}\n",
            encoding="ascii",
        )
        expect_rename_error(
            lambda: rename.inventory_rename(roots, OLD, NEW),
            "destination pin ref exists",
        )


def test_inventory_rejects_stale_and_ambiguous_managed_refs():
    stale_id = "20260828T010203123456Z-old-app-b7be081-deadbeef"
    for kind in ("stale", "ambiguous"):
        with tempfile.TemporaryDirectory() as tmp:
            roots, repo, _, _ = make_fixture(Path(tmp))
            jobs = repo / "refs" / "kilnr" / "jobs"
            if kind == "stale":
                (jobs / stale_id).write_text(SHA + "\n", encoding="ascii")
                expected = "unmatched managed pin ref"
            else:
                (repo / "packed-refs").write_text(
                    f"{'d' * 40} refs/kilnr/jobs/{OLD_ID}\n", encoding="ascii"
                )
                expected = "ambiguous managed pin ref"
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                expected,
            )


def test_inventory_type_checks_every_loose_managed_ref_entry():
    for entry_type in ("symlink", "directory", "fifo", "socket"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots, repo, _, _ = make_fixture(root)
            entry = repo / "refs" / "kilnr" / "jobs" / "unexpected"
            listener = None
            if entry_type == "symlink":
                target = root / "outside-ref"
                target.write_text(SHA + "\n", encoding="ascii")
                entry.symlink_to(target)
            elif entry_type == "directory":
                entry.mkdir()
            elif entry_type == "fifo":
                os.mkfifo(entry)
            else:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(entry))
            try:
                expect_rename_error(
                    lambda: rename.inventory_rename(roots, OLD, NEW),
                    "managed pin ref",
                )
            finally:
                if listener is not None:
                    listener.close()


def test_inventory_rejects_a_pin_ref_that_points_to_the_wrong_sha():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        (repo / "refs" / "kilnr" / "jobs" / OLD_ID).write_text(
            "d" * 40 + "\n", encoding="ascii"
        )
        expect_rename_error(
            lambda: rename.inventory_rename(roots, OLD, NEW),
            "pin ref target mismatch",
        )


def test_inventory_rejects_symlinks_in_managed_locations():
    replacements = (
        lambda roots: roots.config / f"{OLD}.json",
        lambda roots: roots.secrets / f"{OLD}.discord-webhook",
        lambda roots: roots.secrets / OLD,
        lambda roots: roots.state / "cache" / OLD,
        lambda roots: roots.state / "builds" / OLD_ID / "job.json",
        lambda roots: roots.state / "builds" / OLD_ID / "runtime.json",
        lambda roots: roots.state / "builds" / OLD_ID / "status.json",
        lambda roots: roots.state / "builds" / OLD_ID / "pipeline.mk",
    )
    for choose in replacements:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots, _, _, _ = make_fixture(root)
            path = choose(roots)
            target = root / "symlink-target"
            if path.is_dir():
                shutil.rmtree(path)
                target.mkdir()
            else:
                path.unlink()
                target.write_text("target", encoding="utf-8")
            path.symlink_to(target, target_is_directory=target.is_dir())
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "symlink",
            )


def test_inventory_rejects_unexpected_fifo_secret_entries():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, _, _ = make_fixture(Path(tmp))
        fifo = roots.secrets / OLD / "FIFO.value"
        os.mkfifo(fifo)
        expect_rename_error(
            lambda: rename.inventory_rename(roots, OLD, NEW),
            "unexpected secret entry type",
        )


def test_inventory_rejects_unexpected_active_queue_entry_types():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, _, _ = make_fixture(Path(tmp))
        fifo = roots.state / "queue" / "incoming" / "unexpected.json"
        os.mkfifo(fifo)
        expect_rename_error(
            lambda: rename.inventory_rename(roots, OLD, NEW),
            "queue entry is not a regular file",
        )


def test_inventory_rejects_a_symlinked_managed_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        config_link = root / "projects-link"
        config_link.symlink_to(roots.config, target_is_directory=True)
        linked_roots = rename.Roots(
            git=roots.git,
            config=config_link,
            secrets=roots.secrets,
            state=roots.state,
            locks=roots.locks,
        )
        expect_rename_error(
            lambda: rename.inventory_rename(linked_roots, OLD, NEW),
            "symlink",
        )


def test_inventory_rejects_a_symlinked_queue_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        queue = roots.state / "queue"
        real_queue = roots.state / "queue-real"
        queue.rename(real_queue)
        queue.symlink_to(real_queue, target_is_directory=True)
        expect_rename_error(
            lambda: rename.inventory_rename(roots, OLD, NEW),
            "symlink",
        )


def test_inventory_rejects_a_symlinked_refs_kilnr_component():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, repo, _, _ = make_fixture(root)
        shutil.rmtree(repo / "refs" / "kilnr")
        outside = root / "outside-kilnr" / "jobs"
        outside.mkdir(parents=True)
        (outside / OLD_ID).write_text(SHA + "\n", encoding="ascii")
        (repo / "refs" / "kilnr").symlink_to(outside.parent, target_is_directory=True)
        expect_rename_error(
            lambda: rename.inventory_rename(roots, OLD, NEW),
            "repository Kilnr refs is a symlink",
        )


def test_inventory_rejects_malformed_managed_json():
    paths = (
        lambda roots: roots.config / f"{OLD}.json",
        lambda roots: roots.state / "queue" / "incoming" / "bad.json",
        lambda roots: roots.state / "builds" / OLD_ID / "job.json",
        lambda roots: roots.state / "builds" / OLD_ID / "runtime.json",
        lambda roots: roots.state / "builds" / OLD_ID / "status.json",
        lambda roots: roots.secrets / OLD / "TOKEN.json",
    )
    for choose in paths:
        with tempfile.TemporaryDirectory() as tmp:
            roots, _, _, _ = make_fixture(Path(tmp))
            path = choose(roots)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{broken", encoding="utf-8")
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "invalid JSON",
            )


def test_inventory_rejects_inconsistent_repository_config_and_hook():
    mutations = (
        lambda repo: subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "config", "core.bare", "false"],
            check=True,
        ),
        lambda repo: subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "config", "transfer.hideRefs", "refs/other/"],
            check=True,
        ),
        lambda repo: subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "config", "gc.packRefs", "true"],
            check=True,
        ),
        lambda repo: (
            (repo / "hooks" / "post-receive").unlink(),
            (repo / "hooks" / "post-receive").symlink_to(repo / "HEAD"),
        ),
    )
    for mutate in mutations:
        with tempfile.TemporaryDirectory() as tmp:
            roots, repo, _, _ = make_fixture(Path(tmp))
            mutate(repo)
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "repository",
            )


def test_inventory_applies_strict_project_config_validation():
    mutations = (
        lambda config: config["runner"].__setitem__("max_parallel", 0),
        lambda config: config["runner"].__setitem__("memory", "unbounded"),
        lambda config: config["runner"].__setitem__("allowed_networks", []),
        lambda config: config["release"].__setitem__("tag_pattern", "("),
    )
    for mutate in mutations:
        with tempfile.TemporaryDirectory() as tmp:
            roots, _, _, _ = make_fixture(Path(tmp))
            path = roots.config / f"{OLD}.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            mutate(config)
            write_json(path, config)
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "project config",
            )


def test_inventory_rejects_inconsistent_managed_build_schemas():
    mutations = (
        ("job.json", "type"),
        ("runtime.json", "job_type"),
        ("status.json", "state"),
    )
    for filename, missing_key in mutations:
        with tempfile.TemporaryDirectory() as tmp:
            roots, _, build, _ = make_fixture(Path(tmp))
            path = build / filename
            value = json.loads(path.read_text(encoding="utf-8"))
            del value[missing_key]
            write_json(path, value)
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "build",
            )


def test_terminal_selection_and_pre_runtime_failures_rename_successfully():
    for selected_pipeline in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            roots, _, _, _ = make_fixture(Path(tmp))
            failed = make_terminal_prepare_failure(
                roots, selected_pipeline=selected_pipeline
            )
            old_status = json.loads(
                (failed / "status.json").read_text(encoding="utf-8")
            )

            prepared = rename.prepare_rename(
                rename.inventory_rename(roots, OLD, NEW)
            )
            rename.commit_rename(prepared)

            renamed = roots.state / "builds" / NEW_ID_2
            assert renamed.is_dir()
            assert not (renamed / "runtime.json").exists()
            assert not (renamed / "pipeline.mk").exists()
            job = json.loads((renamed / "job.json").read_text(encoding="utf-8"))
            status = json.loads(
                (renamed / "status.json").read_text(encoding="utf-8")
            )
            assert job["id"] == NEW_ID_2
            assert job["project"] == NEW
            assert status["build_id"] == NEW_ID_2
            assert status["job_id"] == NEW_ID_2
            assert status["project"] == NEW
            assert status["pipeline"] is None
            assert status["pipeline_path"] == old_status["pipeline_path"]
            assert (renamed / "logs" / "prepare.log").read_text(
                encoding="utf-8"
            ) == "KILNR ERROR: old-app remains historical\n"


def test_terminal_selection_and_pre_runtime_failures_roll_back_exactly():
    for selected_pipeline in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots, _, _, _ = make_fixture(root)
            make_terminal_prepare_failure(
                roots, selected_pipeline=selected_pipeline
            )
            before = snapshot_tree(root)
            prepared = rename.prepare_rename(
                rename.inventory_rename(roots, OLD, NEW)
            )
            expect_rename_error(
                lambda: rename.commit_rename(
                    prepared,
                    fault=lambda phase: (_ for _ in ()).throw(
                        rename.RenameError("failed-build rollback")
                    )
                    if phase == "verify"
                    else None,
                ),
                "failed-build rollback",
            )
            assert snapshot_tree(root) == before


def test_inventory_validates_managed_build_top_level_entry_types():
    mutations = (
        "missing-commands",
        "symlink-commands",
        "symlink-runtime",
        "symlink-status-lock",
        "unexpected-entry",
    )
    for mutation in mutations:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots, _, build, _ = make_fixture(root)
            if mutation == "missing-commands":
                (build / "commands").rmdir()
            elif mutation == "symlink-commands":
                (build / "commands").rmdir()
                (build / "commands").symlink_to(root / "outside-commands")
            elif mutation == "symlink-runtime":
                (build / "runtime").symlink_to(root / "outside-runtime")
            elif mutation == "symlink-status-lock":
                (build / "status.lock").symlink_to(root / "outside-status-lock")
            else:
                (build / "unexpected-managed-entry").write_text(
                    "unexpected\n", encoding="utf-8"
                )
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "build",
            )


def test_inventory_keeps_commands_and_runtime_payloads_opaque():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, build, _ = make_fixture(Path(tmp))
        runtime = build / "runtime"
        runtime.mkdir(mode=0o750)
        (build / "status.lock").write_text("", encoding="utf-8")
        os.chmod(build / "status.lock", 0o640)
        (build / "commands" / "opaque-link").symlink_to("old-app-outside")
        os.mkfifo(runtime / "opaque-fifo")

        inventory = rename.inventory_rename(roots, OLD, NEW)

        assert inventory.build_ids == {OLD_ID: NEW_ID}


def test_inventory_rejects_invalid_secret_names():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, _, _ = make_fixture(Path(tmp))
        secret_dir = roots.secrets / OLD
        (secret_dir / "bad-name.value").write_bytes(b"private")
        write_json(
            secret_dir / "bad-name.json",
            {"schema": 1, "scope": "release", "kind": "file"},
        )
        expect_rename_error(
            lambda: rename.inventory_rename(roots, OLD, NEW),
            "invalid secret name",
        )


def test_inventory_rejects_unsafe_managed_modes():
    selectors = (
        (lambda roots, repo, build: repo, 0o770),
        (lambda roots, repo, build: roots.config / f"{OLD}.json", 0o664),
        (lambda roots, repo, build: roots.secrets / f"{OLD}.discord-webhook", 0o660),
        (lambda roots, repo, build: roots.secrets / OLD, 0o770),
        (lambda roots, repo, build: roots.secrets / OLD / "TOKEN.json", 0o660),
        (lambda roots, repo, build: repo / "refs" / "kilnr", 0o750),
        (lambda roots, repo, build: repo / "refs" / "kilnr" / "jobs", 0o750),
        (lambda roots, repo, build: roots.state / "cache" / OLD, 0o700),
        (lambda roots, repo, build: build, 0o770),
        (lambda roots, repo, build: build / "job.json", 0o660),
        (lambda roots, repo, build: build / "src", 0o770),
        (lambda roots, repo, build: repo / "refs" / "kilnr" / "jobs" / OLD_ID, 0o666),
    )
    for choose, mode in selectors:
        with tempfile.TemporaryDirectory() as tmp:
            roots, repo, build, _ = make_fixture(Path(tmp))
            path = choose(roots, repo, build)
            os.chmod(path, mode)
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                str(path),
            )


def posix_acl(
    uid,
    *,
    named_permissions=0o7,
    group_permissions=0o5,
    mask_permissions=0o7,
    extra_entries=(),
):
    entries = (
        (0x01, 0o7, 0xFFFFFFFF),
        (0x02, named_permissions, uid),
        (0x04, group_permissions, 0xFFFFFFFF),
    ) + tuple(extra_entries) + (
        (0x10, mask_permissions, 0xFFFFFFFF),
        (0x20, 0o0, 0xFFFFFFFF),
    )
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, entry_id)
        for tag, permissions, entry_id in entries
    )


def posix_file_acl(uid, *, named_permissions=0o7, extra_entries=()):
    entries = (
        (0x01, 0o6, 0xFFFFFFFF),
        (0x02, named_permissions, uid),
        (0x04, 0o5, 0xFFFFFFFF),
    ) + tuple(extra_entries) + (
        (0x10, 0o6, 0xFFFFFFFF),
        (0x20, 0o0, 0xFFFFFFFF),
    )
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, entry_id)
        for tag, permissions, entry_id in entries
    )


@contextlib.contextmanager
def synthetic_production_ref_policy(uid):
    directory_acl = (
        ("system.posix_acl_access", posix_acl(uid)),
        ("system.posix_acl_default", posix_acl(uid)),
    )
    acl_by_inode = {}
    original_read_acl = rename._read_acl
    original_setxattr = getattr(rename.os, "setxattr", None)
    original_listxattr = getattr(rename.os, "listxattr", None)
    original_removexattr = getattr(rename.os, "removexattr", None)
    original_uid = rename._kilnr_acl_uid

    def inode_for(target):
        info = os.fstat(target) if isinstance(target, int) else Path(target).lstat()
        return info.st_dev, info.st_ino

    def read_acl(path):
        selected = Path(path)
        try:
            inode = inode_for(selected)
        except FileNotFoundError:
            return ()
        stored = tuple(
            sorted(
                (name, value)
                for (device, number, name), value in acl_by_inode.items()
                if (device, number) == inode
            )
        )
        if stored:
            return stored
        if selected.parts[-2:] == ("refs", "kilnr"):
            return directory_acl
        if selected.parts[-3:] == ("refs", "kilnr", "jobs"):
            return directory_acl
        return ()

    def capture_acl(target, name, value, *args, **kwargs):
        if name.startswith("system.posix_acl_"):
            device, number = inode_for(target)
            acl_by_inode[(device, number, name)] = value
            return None
        if original_setxattr is None:
            raise AttributeError("os.setxattr is unavailable")
        return original_setxattr(target, name, value, *args, **kwargs)

    def list_acl(target, *args, **kwargs):
        device, number = inode_for(target)
        stored = sorted(
            name
            for stored_device, stored_number, name in acl_by_inode
            if (stored_device, stored_number) == (device, number)
        )
        if original_listxattr is None:
            return stored
        actual = [
            os.fsdecode(name)
            for name in original_listxattr(target, *args, **kwargs)
        ]
        return sorted(set(actual) | set(stored))

    def remove_acl(target, name, *args, **kwargs):
        normalized = os.fsdecode(name)
        device, number = inode_for(target)
        key = (device, number, normalized)
        if key in acl_by_inode:
            del acl_by_inode[key]
            return None
        if original_removexattr is None:
            raise OSError(getattr(errno, "ENODATA", 61), "attribute is absent")
        return original_removexattr(target, name, *args, **kwargs)

    rename._read_acl = read_acl
    rename.os.setxattr = capture_acl
    rename.os.listxattr = list_acl
    rename.os.removexattr = remove_acl
    rename._kilnr_acl_uid = lambda _roots: uid
    try:
        yield read_acl
    finally:
        rename._read_acl = original_read_acl
        if original_setxattr is None:
            delattr(rename.os, "setxattr")
        else:
            rename.os.setxattr = original_setxattr
        if original_listxattr is None:
            delattr(rename.os, "listxattr")
        else:
            rename.os.listxattr = original_listxattr
        if original_removexattr is None:
            delattr(rename.os, "removexattr")
        else:
            rename.os.removexattr = original_removexattr
        rename._kilnr_acl_uid = original_uid


def test_inventory_accepts_project_create_acl_mask_and_execute_cache_parent_modes():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        assert stat.S_IMODE((repo / "refs" / "kilnr").stat().st_mode) == 0o770
        assert stat.S_IMODE((repo / "refs" / "kilnr" / "jobs").stat().st_mode) == 0o770
        assert stat.S_IMODE((roots.state / "cache" / OLD).stat().st_mode) == 0o750
        rename.inventory_rename(roots, OLD, NEW)


def test_inventory_validates_project_create_named_user_acl_policy():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        acl_paths = {
            repo / "refs" / "kilnr",
            repo / "refs" / "kilnr" / "jobs",
        }
        loose = repo / "refs" / "kilnr" / "jobs" / OLD_ID
        os.chmod(loose, 0o660)
        uid = os.getuid()
        valid_acl = (
            ("system.posix_acl_access", posix_acl(uid)),
            ("system.posix_acl_default", posix_acl(uid)),
        )
        original_acl = rename._read_acl
        original_uid = rename._kilnr_acl_uid
        rename._kilnr_acl_uid = lambda fixture_roots: uid
        rename._read_acl = lambda path: (
            valid_acl
            if Path(path) in acl_paths
            else (("system.posix_acl_access", posix_file_acl(uid)),)
            if Path(path) == loose
            else ()
        )
        try:
            rename.inventory_rename(roots, OLD, NEW)
            invalid_acl = (
                ("system.posix_acl_access", posix_acl(uid, named_permissions=0o5)),
                ("system.posix_acl_default", posix_acl(uid)),
            )
            rename._read_acl = lambda path: (
                invalid_acl
                if Path(path) in acl_paths
                else (("system.posix_acl_access", posix_file_acl(uid)),)
                if Path(path) == loose
                else ()
            )
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "ACL",
            )
        finally:
            rename._read_acl = original_acl
            rename._kilnr_acl_uid = original_uid


def test_inventory_accepts_producer_authentic_loose_ref_acl_policy():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        loose = repo / "refs" / "kilnr" / "jobs" / OLD_ID
        os.chmod(loose, 0o660)
        uid = os.getuid() + 1000
        ref_directories = {
            repo / "refs" / "kilnr",
            repo / "refs" / "kilnr" / "jobs",
        }
        valid_directory_acl = (
            ("system.posix_acl_access", posix_acl(uid)),
            ("system.posix_acl_default", posix_acl(uid)),
        )
        valid_file_acl = (
            ("system.posix_acl_access", posix_file_acl(uid)),
        )
        original_acl = rename._read_acl
        original_uid = rename._kilnr_acl_uid
        rename._kilnr_acl_uid = lambda fixture_roots: uid
        rename._read_acl = lambda path: (
            valid_directory_acl
            if Path(path) in ref_directories
            else valid_file_acl
            if Path(path) == loose
            else ()
        )
        try:
            inventory = rename.inventory_rename(roots, OLD, NEW)
        finally:
            rename._read_acl = original_acl
            rename._kilnr_acl_uid = original_uid
        assert inventory.source_facts[loose].mode == 0o660


def test_git_created_loose_ref_inherits_authentic_acl_on_linux():
    if not sys.platform.startswith("linux") or not hasattr(os, "setxattr"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        roots = make_roots(Path(tmp))
        repo = make_project(roots)
        make_build(roots)
        uid = os.getuid()
        directory_acl = posix_acl(uid)
        try:
            for path in (
                repo / "refs" / "kilnr",
                repo / "refs" / "kilnr" / "jobs",
            ):
                os.setxattr(path, "system.posix_acl_access", directory_acl)
                os.setxattr(path, "system.posix_acl_default", directory_acl)
            subprocess.run(
                [
                    "/usr/bin/git",
                    f"--git-dir={repo}",
                    "update-ref",
                    f"refs/kilnr/jobs/{OLD_ID}",
                    SHA,
                ],
                check=True,
            )
        except OSError as exc:
            if exc.errno in (getattr(os, "ENOTSUP", 95), 95, 45, 1):
                return
            raise
        loose = repo / "refs" / "kilnr" / "jobs" / OLD_ID
        actual_acl = os.getxattr(loose, "system.posix_acl_access")
        assert stat.S_IMODE(loose.stat().st_mode) == 0o660
        assert set(rename._decode_posix_acl(actual_acl, loose, "access")) == set(
            rename._decode_posix_acl(posix_file_acl(uid), loose, "expected")
        )
        original_uid = rename._kilnr_acl_uid
        rename._kilnr_acl_uid = lambda fixture_roots: uid
        try:
            rename.inventory_rename(roots, OLD, NEW)
        finally:
            rename._kilnr_acl_uid = original_uid


def test_real_linux_packed_only_ref_acl_commit_and_exact_rollback():
    if (
        not sys.platform.startswith("linux")
        or not all(hasattr(os, name) for name in ("getxattr", "setxattr"))
    ):
        return
    unsupported = {
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOSYS", -1),
    }

    for outcome in ("commit", "rollback"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots, repo, _, _ = make_fixture(root)
            acl_uid = os.getuid() + 1000
            directory_acl = posix_acl(acl_uid)
            try:
                for path in (
                    repo / "refs" / "kilnr",
                    repo / "refs" / "kilnr" / "jobs",
                ):
                    os.setxattr(path, "system.posix_acl_access", directory_acl)
                    os.setxattr(path, "system.posix_acl_default", directory_acl)
            except OSError as exc:
                if exc.errno in unsupported:
                    return
                raise

            old_ref = repo / "refs" / "kilnr" / "jobs" / OLD_ID
            old_ref.unlink()
            subprocess.run(
                [
                    "/usr/bin/git",
                    f"--git-dir={repo}",
                    "update-ref",
                    f"refs/kilnr/jobs/{OLD_ID}",
                    SHA,
                ],
                check=True,
            )
            assert stat.S_IMODE(old_ref.stat().st_mode) == 0o660
            source_acl = os.getxattr(old_ref, "system.posix_acl_access")
            assert set(rename._decode_posix_acl(source_acl, old_ref, "source")) == set(
                rename._decode_posix_acl(
                    posix_file_acl(acl_uid),
                    old_ref,
                    "expected",
                )
            )
            assert set(dict(rename._read_acl(old_ref))) == {"system.posix_acl_access"}

            subprocess.run(
                ["/usr/bin/git", f"--git-dir={repo}", "pack-refs", "--all", "--prune"],
                check=True,
            )
            os.chmod(repo / "packed-refs", 0o600)
            assert not old_ref.exists()

            if outcome == "commit":
                prepared = rename.prepare_rename(
                    rename.inventory_rename(roots, OLD, NEW)
                )
                rename.commit_rename(prepared)
                new_repo = roots.git / f"{NEW}.git"
                new_ref = new_repo / "refs" / "kilnr" / "jobs" / NEW_ID
                assert stat.S_IMODE(new_ref.stat().st_mode) == 0o660
                actual_acl = os.getxattr(new_ref, "system.posix_acl_access")
                assert set(
                    rename._decode_posix_acl(actual_acl, new_ref, "destination")
                ) == set(
                    rename._decode_posix_acl(
                        posix_file_acl(acl_uid),
                        new_ref,
                        "expected",
                    )
                )
                assert set(dict(rename._read_acl(new_ref))) == {
                    "system.posix_acl_access"
                }
                rename.verify_rename(roots, OLD, NEW, {OLD_ID: NEW_ID})
                continue

            warmup = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
            warmup.cleanup()
            before = snapshot_tree_with_real_acls(root)
            prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
            try:
                rename.commit_rename(
                    prepared,
                    fault=lambda phase: (_ for _ in ()).throw(
                        rename.RenameError("injected real ACL rollback")
                    ) if phase == "verify" else None,
                )
            except rename.RenameError as exc:
                assert "injected real ACL rollback" in str(exc), str(exc)
                assert "rollback failed" not in str(exc), str(exc)
            else:
                raise AssertionError("expected forced real ACL rollback")
            after = snapshot_tree_with_real_acls(root)
            if after != before:
                before_tree = {record[1]: record for record in before[0]}
                after_tree = {record[1]: record for record in after[0]}
                before_acl = dict(before[1])
                after_acl = dict(after[1])
                tree_changes = sorted(
                    path
                    for path in set(before_tree) | set(after_tree)
                    if before_tree.get(path) != after_tree.get(path)
                )
                acl_changes = sorted(
                    path
                    for path in set(before_acl) | set(after_acl)
                    if before_acl.get(path) != after_acl.get(path)
                )
                raise AssertionError(
                    "real ACL rollback was not exact: "
                    f"tree={tree_changes!r}, ACL={acl_changes!r}"
                )
            assert ref_value(repo, f"refs/kilnr/jobs/{OLD_ID}") == SHA
            assert ref_value(repo, f"refs/kilnr/jobs/{NEW_ID}") is None
            assert not old_ref.exists()
            assert not (roots.git / f"{NEW}.git").exists()
            rename.inventory_rename(roots, OLD, NEW)


def assert_ref_acl_mutation_rejected(*, extra_entries=(), group_permissions=0o5):
    for relative in (Path("refs/kilnr"), Path("refs/kilnr/jobs")):
        for acl_name in ("system.posix_acl_access", "system.posix_acl_default"):
            with tempfile.TemporaryDirectory() as tmp:
                roots, repo, _, _ = make_fixture(Path(tmp))
                acl_paths = {
                    repo / "refs" / "kilnr",
                    repo / "refs" / "kilnr" / "jobs",
                }
                target = repo / relative
                loose = repo / "refs" / "kilnr" / "jobs" / OLD_ID
                os.chmod(loose, 0o660)
                uid = os.getuid()
                valid = posix_acl(uid)
                invalid = posix_acl(
                    uid,
                    group_permissions=group_permissions,
                    extra_entries=extra_entries,
                )
                acl_by_path = {
                    path: (
                        (
                            "system.posix_acl_access",
                            invalid if path == target and acl_name.endswith("access") else valid,
                        ),
                        (
                            "system.posix_acl_default",
                            invalid if path == target and acl_name.endswith("default") else valid,
                        ),
                    )
                    for path in acl_paths
                }
                original_acl = rename._read_acl
                original_uid = rename._kilnr_acl_uid
                rename._kilnr_acl_uid = lambda fixture_roots: uid
                rename._read_acl = lambda path: (
                    acl_by_path[Path(path)]
                    if Path(path) in acl_by_path
                    else (("system.posix_acl_access", posix_file_acl(uid)),)
                    if Path(path) == loose
                    else ()
                )
                try:
                    expect_rename_error(
                        lambda: rename.inventory_rename(roots, OLD, NEW),
                        "ACL policy",
                    )
                finally:
                    rename._read_acl = original_acl
                    rename._kilnr_acl_uid = original_uid


def test_inventory_rejects_extra_named_user_in_each_managed_ref_acl():
    assert_ref_acl_mutation_rejected(extra_entries=((0x02, 0o7, os.getuid() + 1),))


def test_inventory_rejects_extra_named_group_in_each_managed_ref_acl():
    assert_ref_acl_mutation_rejected(extra_entries=((0x08, 0o7, os.getgid() + 1),))


def test_inventory_rejects_writable_owning_group_in_each_managed_ref_acl():
    assert_ref_acl_mutation_rejected(group_permissions=0o7)


def test_inventory_rejects_untrusted_managed_hook_owner():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, _, _ = make_fixture(Path(tmp))
        hook = ROOT / "libexec" / "git-hooks" / "post-receive"
        original_lstat = rename._lstat

        def different_owner(path, description):
            info = original_lstat(path, description)
            if Path(path) != hook:
                return info
            fields = list(info)
            fields[4] += 1
            return os.stat_result(fields)

        rename._lstat = different_owner
        try:
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "post-receive hook ownership",
            )
        finally:
            rename._lstat = original_lstat


def test_production_managed_hook_policy_requires_root_root():
    assert rename._managed_hook_owner(rename.DEFAULT_ROOTS) == (0, 0)


def test_inventory_rejects_inconsistent_managed_ownership():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, _, _ = make_fixture(Path(tmp))
        config_path = roots.config / f"{OLD}.json"
        original_lstat = rename._lstat

        def different_owner(path, description):
            info = original_lstat(path, description)
            if Path(path) != config_path:
                return info
            fields = list(info)
            fields[4] += 1
            return os.stat_result(fields)

        rename._lstat = different_owner
        try:
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "unexpected ownership",
            )
        finally:
            rename._lstat = original_lstat


def test_production_repository_policy_rejects_a_kilnr_owned_repository():
    with tempfile.TemporaryDirectory() as tmp:
        roots = make_roots(Path(tmp))
        repo = make_project(roots)
        actual_owner = (repo.stat().st_uid, repo.stat().st_gid)
        kilnr_owner = (actual_owner[0] + 1000, actual_owner[1] + 1000)
        expect_rename_error(
            lambda: rename._validate_bare_repository(
                repo,
                {},
                managed_hook_owner=(
                    (ROOT / "libexec" / "git-hooks" / "post-receive").stat().st_uid,
                    (ROOT / "libexec" / "git-hooks" / "post-receive").stat().st_gid,
                ),
                kilnr_acl_uid=None,
                expected_owner=kilnr_owner,
                kilnr_identity=None,
            ),
            "ownership",
        )


def test_production_repository_policy_rejects_kilnr_writable_refs_heads():
    with tempfile.TemporaryDirectory() as tmp:
        roots = make_roots(Path(tmp))
        repo = make_project(roots)
        heads = repo / "refs" / "heads"
        kilnr_uid = os.getuid() + 1000
        original_acl = rename._read_acl
        rename._read_acl = lambda path: (
            (("system.posix_acl_access", posix_acl(kilnr_uid)),)
            if Path(path) == heads
            else ()
        )
        try:
            expect_rename_error(
                lambda: rename._validate_bare_repository(
                    repo,
                    {},
                    managed_hook_owner=(
                        (ROOT / "libexec" / "git-hooks" / "post-receive").stat().st_uid,
                        (ROOT / "libexec" / "git-hooks" / "post-receive").stat().st_gid,
                    ),
                    kilnr_acl_uid=None,
                    expected_owner=(repo.stat().st_uid, repo.stat().st_gid),
                    kilnr_identity=(kilnr_uid, frozenset({os.getgid()})),
                ),
                "kilnr can write refs/heads",
            )
        finally:
            rename._read_acl = original_acl


def test_production_metadata_policy_rejects_extra_acl_writers():
    path = Path("/etc/kilnr/projects/demo.json")
    writer_uid = os.getuid() + 1000
    facts = rename.FileFacts(
        mode=0o640,
        uid=0,
        gid=0,
        acl=(("system.posix_acl_access", posix_file_acl(writer_uid)),),
    )
    expect_rename_error(
        lambda: rename._validate_no_extra_metadata_writers(path, facts),
        "metadata writer",
    )


def test_production_root_policy_rejects_wrong_owner_group_and_mode():
    with tempfile.TemporaryDirectory() as tmp:
        roots = make_roots(Path(tmp))
        uid = os.getuid()
        gid = os.getgid()
        policy = rename.ProductionPolicy(
            root=(uid, gid),
            git=(uid, gid),
            kilnr=(uid, gid),
            submit_gid=gid,
            kilnr_groups=frozenset({gid}),
        )
        expected_modes = {
            roots.git: 0o755,
            roots.config: 0o755,
            roots.secrets: 0o750,
            roots.state: 0o710,
            roots.locks.parent: 0o750,
            roots.locks: 0o2750,
            roots.state / "queue": 0o710,
            roots.state / "builds": 0o750,
            roots.state / "cache": 0o700,
        }
        original_lstat = rename._lstat
        mutation = {
            "wrong_group": False,
            "wrong_mode": False,
            "wrong_state_owner": False,
        }

        def production_facts(path, description):
            info = original_lstat(path, description)
            selected = Path(path)
            if selected not in expected_modes:
                return info
            fields = list(info)
            fields[0] = stat.S_IFMT(info.st_mode) | expected_modes[selected]
            if mutation["wrong_group"] and selected == roots.secrets:
                fields[5] += 1
            if mutation["wrong_state_owner"] and selected == roots.state:
                fields[4] += 1
            if mutation["wrong_mode"] and selected == roots.locks:
                fields[0] = stat.S_IFMT(info.st_mode) | 0o2770
            return os.stat_result(fields)

        rename._lstat = production_facts
        try:
            rename._validate_roots(roots, policy)
            mutation["wrong_group"] = True
            expect_rename_error(
                lambda: rename._validate_roots(roots, policy),
                "ownership",
            )
            mutation["wrong_group"] = False
            mutation["wrong_state_owner"] = True
            expect_rename_error(
                lambda: rename._validate_roots(roots, policy),
                "ownership",
            )
            mutation["wrong_state_owner"] = False
            mutation["wrong_mode"] = True
            expect_rename_error(
                lambda: rename._validate_roots(roots, policy),
                "mode",
            )
        finally:
            rename._lstat = original_lstat


def test_inventory_captures_acl_facts_for_managed_sources():
    assert hasattr(rename, "_read_acl"), "rename inventory has no ACL reader"
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, _, _ = make_fixture(Path(tmp))
        original = rename._read_acl
        rename._read_acl = lambda path: (("test.acl", str(path).encode("utf-8")),)
        try:
            inventory = rename.inventory_rename(roots, OLD, NEW)
        finally:
            rename._read_acl = original
        assert inventory.source_facts[repo].acl == (("test.acl", str(repo).encode("utf-8")),)
        assert inventory.source_facts[roots.config / f"{OLD}.json"].mode == 0o644


def test_inventory_rejects_build_ids_not_anchored_to_structured_metadata():
    malformed_ids = (
        "20260828T010203123456Z-old-app-deadbee-0123abcd",
        "20260828T010203123456Z-old-app-b7be081-nothex00",
        "20260828T010203123456Z-prefix-old-app-b7be081-0123abcd",
        "20260828T010203123457Z-old-app-b7be081-0123abcd",
    )
    for malformed_id in malformed_ids:
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_roots(Path(tmp))
            make_project(roots)
            make_build(roots, build_id=malformed_id)
            expect_rename_error(
                lambda: rename.inventory_rename(roots, OLD, NEW),
                "invalid build id",
            )


def test_prepare_rewrites_only_allowlisted_metadata_and_preserves_attributes():
    with tempfile.TemporaryDirectory() as tmp:
        roots, repo, build, original_bytes = make_fixture(Path(tmp))
        config = roots.config / f"{OLD}.json"
        original_stat = config.stat()
        inventory = rename.inventory_rename(roots, OLD, NEW)

        prepared = rename.prepare_rename(inventory)
        try:
            config_bytes, prepared_config = prepared_bytes(
                prepared, roots.config / f"{NEW}.json"
            )
            rewritten_config = json.loads(config_bytes)
            assert rewritten_config["project"] == NEW
            assert rewritten_config["repository"] == str(roots.git / f"{NEW}.git")
            assert rewritten_config["discord"]["webhook_file"] == str(
                roots.secrets / f"{NEW}.discord-webhook"
            )
            prepared_stat = prepared_config.temporary.stat()
            assert stat.S_IMODE(prepared_stat.st_mode) == 0o644
            assert (prepared_stat.st_uid, prepared_stat.st_gid) == (
                original_stat.st_uid,
                original_stat.st_gid,
            )

            rewritten_job = json.loads(
                prepared_bytes(prepared, roots.state / "builds" / NEW_ID / "job.json")[0]
            )
            assert rewritten_job["id"] == NEW_ID
            assert rewritten_job["project"] == NEW
            assert rewritten_job["pin_ref"] == f"refs/kilnr/jobs/{NEW_ID}"
            assert rewritten_job["ref"] == "refs/heads/feature/old-app-history"

            pipeline = prepared_bytes(
                prepared, roots.state / "builds" / NEW_ID / "pipeline.mk"
            )[0].decode("utf-8")
            assert f"execute {NEW_ID} test" in pipeline
            assert OLD_ID not in pipeline

            assert (build / "artifacts" / "old-app.bin").read_bytes() == original_bytes
            assert (build / "src" / "identity.txt").read_text(encoding="utf-8") == OLD_ID
            assert (build / "work" / "old-app.bin").read_bytes() == original_bytes
            assert (build / "logs" / "old-app.log").read_bytes() == original_bytes
            assert (roots.state / "cache" / OLD / "opaque.bin").read_bytes() == original_bytes
            assert (roots.secrets / OLD / "TOKEN.value").read_bytes() == b"old-app\x00\xffprivate"
            assert (repo / "objects" / "old-app-object").read_bytes() == original_bytes
        finally:
            prepared.cleanup()
        assert not any(path.name.endswith(".rename-tmp") for path in Path(tmp).rglob("*"))


def test_prepare_cleans_every_temp_after_injected_failures():
    for failure_point in ("write", "json", "file_fsync", "directory_fsync"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots, _, _, _ = make_fixture(root)
            inventory = rename.inventory_rename(roots, OLD, NEW)
            restore = []
            if failure_point == "write":
                original = rename.os.write
                calls = [0]

                def fail_second_write(fd, data):
                    calls[0] += 1
                    if calls[0] == 2:
                        raise OSError("injected write failure")
                    return original(fd, data)

                rename.os.write = fail_second_write
                restore.append(lambda: setattr(rename.os, "write", original))
            elif failure_point == "json":
                original = rename._read_json
                calls = [0]

                def fail_second_validation(path, description):
                    if path.name.endswith(".rename-tmp"):
                        calls[0] += 1
                        if calls[0] == 2:
                            raise rename.RenameError("injected JSON validation failure")
                    return original(path, description)

                rename._read_json = fail_second_validation
                restore.append(lambda: setattr(rename, "_read_json", original))
            elif failure_point == "file_fsync":
                original = rename.os.fsync
                calls = [0]

                def fail_second_file_fsync(fd):
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        calls[0] += 1
                        if calls[0] == 2:
                            raise OSError("injected file fsync failure")
                    return original(fd)

                rename.os.fsync = fail_second_file_fsync
                restore.append(lambda: setattr(rename.os, "fsync", original))
            else:
                original = rename._fsync_directory
                calls = [0]

                def fail_second_directory_fsync(path):
                    calls[0] += 1
                    if calls[0] == 2:
                        raise OSError("injected directory fsync failure")
                    return original(path)

                rename._fsync_directory = fail_second_directory_fsync
                restore.append(lambda: setattr(rename, "_fsync_directory", original))
            try:
                try:
                    rename.prepare_rename(inventory)
                except (OSError, rename.RenameError):
                    pass
                else:
                    raise AssertionError(f"expected injected {failure_point} failure")
            finally:
                for action in reversed(restore):
                    action()
            assert staged_files(root) == [], failure_point


def test_prepared_cleanup_removes_files_before_and_after_a_build_parent_move():
    for move_parent in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots, _, _, _ = make_fixture(root)
            inventory = rename.inventory_rename(roots, OLD, NEW)
            prepared = rename.prepare_rename(inventory)
            assert staged_files(root)
            if move_parent:
                inventory.builds[0].source.rename(inventory.builds[0].destination)
            prepared.cleanup()
            assert staged_files(root) == []


def test_prepare_applies_fchown_before_the_final_fchmod():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, _, _ = make_fixture(Path(tmp))
        inventory = rename.inventory_rename(roots, OLD, NEW)
        write = inventory.metadata_writes[0]
        original_fstat = rename.os.fstat
        original_fchown = rename.os.fchown
        original_fchmod = rename.os.fchmod
        first_fstat = [True]
        events = []

        def different_owner(fd):
            info = original_fstat(fd)
            if not first_fstat[0]:
                return info
            first_fstat[0] = False
            fields = list(info)
            fields[4] += 1
            fields[5] += 1
            return os.stat_result(fields)

        rename.os.fstat = different_owner
        rename.os.fchown = lambda fd, uid, gid: events.append(("fchown", uid, gid))
        def record_fchmod(fd, mode):
            events.append(("fchmod", mode))
            original_fchmod(fd, mode)

        rename.os.fchmod = record_fchmod
        prepared_file = None
        try:
            prepared_file = rename._prepare_write(write)
        finally:
            rename.os.fstat = original_fstat
            rename.os.fchown = original_fchown
            rename.os.fchmod = original_fchmod
            if prepared_file is not None:
                prepared_file.temporary.unlink(missing_ok=True)
        assert [event[0] for event in events[:2]] == ["fchown", "fchmod"]
        assert events[0][1:] == (write.source.stat().st_uid, write.source.stat().st_gid)
        assert events[1][1] == stat.S_IMODE(write.source.stat().st_mode)


def test_prepare_refuses_permission_drift_after_inventory():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        inventory = rename.inventory_rename(roots, OLD, NEW)
        os.chmod(roots.config / f"{OLD}.json", 0o600)
        expect_rename_error(
            lambda: rename.prepare_rename(inventory),
            "permissions changed after inventory",
        )
        assert staged_files(root) == []


def test_prepare_copies_captured_acl_data_to_staged_files():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, _, _ = make_fixture(Path(tmp))
        original_read_acl = rename._read_acl
        had_setxattr = hasattr(rename.os, "setxattr")
        original_setxattr = getattr(rename.os, "setxattr", None)
        calls = []
        rename._read_acl = lambda path: (("test.acl", b"marker"),)
        rename.os.setxattr = lambda fd, name, value: calls.append((name, value))
        prepared_file = None
        try:
            inventory = rename.inventory_rename(roots, OLD, NEW)
            prepared_file = rename._prepare_write(inventory.metadata_writes[0])
        finally:
            rename._read_acl = original_read_acl
            if had_setxattr:
                rename.os.setxattr = original_setxattr
            else:
                delattr(rename.os, "setxattr")
            if prepared_file is not None:
                prepared_file.temporary.unlink(missing_ok=True)
        assert calls == [("test.acl", b"marker")]


def test_prepare_rejects_pipeline_entries_with_an_unexpected_build_id():
    with tempfile.TemporaryDirectory() as tmp:
        roots, _, build, _ = make_fixture(Path(tmp))
        pipeline = build / "pipeline.mk"
        pipeline.write_text(
            pipeline.read_text(encoding="utf-8").replace(OLD_ID, "another-build"),
            encoding="utf-8",
        )
        expect_rename_error(
            lambda: rename.inventory_rename(roots, OLD, NEW),
            "pipeline.mk",
        )


def test_commit_renames_all_managed_state_and_preserves_opaque_payloads():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, original_bytes, prepared = prepare_fixture(root)

        rename.commit_rename(prepared)

        new_repo = roots.git / f"{NEW}.git"
        new_build = roots.state / "builds" / NEW_ID
        assert not (roots.git / f"{OLD}.git").exists()
        assert not (roots.config / f"{OLD}.json").exists()
        assert not (roots.secrets / f"{OLD}.discord-webhook").exists()
        assert not (roots.secrets / OLD).exists()
        assert not (roots.state / "cache" / OLD).exists()
        assert not (roots.state / "builds" / OLD_ID).exists()
        assert new_repo.is_dir()
        assert (roots.config / f"{NEW}.json").is_file()
        assert (roots.secrets / f"{NEW}.discord-webhook").is_file()
        assert (roots.secrets / NEW).is_dir()
        assert (roots.state / "cache" / NEW).is_dir()
        assert new_build.is_dir()

        config = json.loads((roots.config / f"{NEW}.json").read_text(encoding="utf-8"))
        assert config["project"] == NEW
        assert config["repository"] == str(new_repo)
        assert config["discord"]["webhook_file"] == str(
            roots.secrets / f"{NEW}.discord-webhook"
        )
        for filename in ("job.json", "runtime.json", "status.json"):
            metadata = json.loads((new_build / filename).read_text(encoding="utf-8"))
            assert metadata["project"] == NEW
            assert OLD_ID not in {
                metadata.get("id"), metadata.get("build_id"), metadata.get("job_id")
            }
        assert NEW_ID in (new_build / "pipeline.mk").read_text(encoding="utf-8")
        assert (new_build / "artifacts" / "old-app.bin").read_bytes() == original_bytes
        assert (new_build / "src" / "identity.txt").read_text(encoding="utf-8") == OLD_ID
        assert (new_build / "work" / "old-app.bin").read_bytes() == original_bytes
        assert (new_build / "logs" / "old-app.log").read_bytes() == original_bytes
        assert (roots.state / "cache" / NEW / "opaque.bin").read_bytes() == original_bytes
        assert (roots.secrets / NEW / "TOKEN.value").read_bytes() == b"old-app\x00\xffprivate"
        assert (new_repo / "objects" / "old-app-object").read_bytes() == original_bytes
        assert ref_value(new_repo, f"refs/kilnr/jobs/{OLD_ID}") is None
        assert ref_value(new_repo, f"refs/kilnr/jobs/{NEW_ID}") == SHA
        new_ref = new_repo / "refs" / "kilnr" / "jobs" / NEW_ID
        assert stat.S_IMODE(new_ref.stat().st_mode) == 0o640
        assert staged_files(root) == []
        assert not [path for path in root.rglob("*") if "rename-rollback" in path.name]
        rename.verify_rename(roots, OLD, NEW, {OLD_ID: NEW_ID})


def test_packed_only_commit_materializes_the_fixture_loose_ref_policy():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, repo, _, _ = make_fixture(root)
        subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "pack-refs", "--all", "--prune"],
            check=True,
        )
        packed_refs = repo / "packed-refs"
        os.chmod(packed_refs, 0o600)
        assert not (repo / "refs" / "kilnr" / "jobs" / OLD_ID).exists()

        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
        rename.commit_rename(prepared)

        new_repo = roots.git / f"{NEW}.git"
        new_ref = new_repo / "refs" / "kilnr" / "jobs" / NEW_ID
        assert stat.S_IMODE(new_ref.stat().st_mode) == 0o640
        assert stat.S_IMODE((new_repo / "packed-refs").stat().st_mode) == 0o600
        rename.verify_rename(roots, OLD, NEW, {OLD_ID: NEW_ID})


def test_packed_only_commit_materializes_the_production_loose_ref_policy():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, repo, _, _ = make_fixture(root)
        subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "pack-refs", "--all", "--prune"],
            check=True,
        )
        os.chmod(repo / "packed-refs", 0o600)
        uid = os.getuid() + 1000

        with synthetic_production_ref_policy(uid) as read_acl:
            prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
            rename.commit_rename(prepared)

            new_repo = roots.git / f"{NEW}.git"
            new_ref = new_repo / "refs" / "kilnr" / "jobs" / NEW_ID
            assert stat.S_IMODE(new_ref.stat().st_mode) == 0o660
            acl = dict(read_acl(new_ref))
            assert set(acl) == {"system.posix_acl_access"}
            assert set(
                rename._decode_posix_acl(
                    acl["system.posix_acl_access"],
                    new_ref,
                    "system.posix_acl_access",
                )
            ) == set(
                rename._decode_posix_acl(
                    posix_file_acl(uid),
                    new_ref,
                    "expected",
                )
            )
            rename.verify_rename(roots, OLD, NEW, {OLD_ID: NEW_ID})


def test_commit_rolls_back_exactly_after_every_stable_phase():
    for phase in rename.COMMIT_PHASES:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots, _, _, _, prepared = prepare_fixture(root)
            before = snapshot_tree(root)
            prepared.cleanup()
            before = snapshot_tree(root)
            prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))

            def fail_at(current):
                if current == phase:
                    raise rename.RenameError(f"injected failure at {phase}")

            expect_rename_error(
                lambda: rename.commit_rename(prepared, fault=fail_at),
                f"injected failure at {phase}",
            )
            assert snapshot_tree(root) == before, phase
            assert staged_files(root) == [], phase


def test_commit_rolls_back_a_packed_source_ref_with_update_ref():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, repo, _, _ = make_fixture(root)
        subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "pack-refs", "--all", "--prune"],
            check=True,
        )
        assert not (repo / "refs" / "kilnr" / "jobs" / OLD_ID).exists()
        before = snapshot_tree(root)
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))

        def fail_after_verification(phase):
            if phase == "verify":
                raise rename.RenameError("injected packed-ref failure")

        expect_rename_error(
            lambda: rename.commit_rename(prepared, fault=fail_after_verification),
            "injected packed-ref failure",
        )
        old_repo = roots.git / f"{OLD}.git"
        assert ref_value(old_repo, f"refs/kilnr/jobs/{OLD_ID}") == SHA
        assert ref_value(old_repo, f"refs/kilnr/jobs/{NEW_ID}") is None
        assert not (roots.git / f"{NEW}.git").exists()
        assert staged_files(root) == []
        assert snapshot_tree(root) == before
        rename.inventory_rename(roots, OLD, NEW)


def test_packed_only_production_policy_rolls_back_exactly_after_verification():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, repo, _, _ = make_fixture(root)
        subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "pack-refs", "--all", "--prune"],
            check=True,
        )
        os.chmod(repo / "packed-refs", 0o600)
        before = snapshot_tree(root)
        uid = os.getuid() + 1000

        with synthetic_production_ref_policy(uid):
            prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
            expect_rename_error(
                lambda: rename.commit_rename(
                    prepared,
                    fault=lambda phase: (_ for _ in ()).throw(
                        rename.RenameError("injected production packed-ref rollback")
                    ) if phase == "verify" else None,
                ),
                "injected production packed-ref rollback",
            )
            assert snapshot_tree(root) == before
            assert ref_value(repo, f"refs/kilnr/jobs/{OLD_ID}") == SHA
            assert ref_value(repo, f"refs/kilnr/jobs/{NEW_ID}") is None
            rename.inventory_rename(roots, OLD, NEW)


def test_commit_reports_failed_rollback_with_both_exact_paths():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _, prepared = prepare_fixture(root)
        old_repo = roots.git / f"{OLD}.git"
        new_repo = roots.git / f"{NEW}.git"
        original_replace = rename.os.replace

        def fail_repo_inverse(source, destination):
            if Path(source) == new_repo and Path(destination) == old_repo:
                raise OSError("injected inverse failure")
            return original_replace(source, destination)

        rename.os.replace = fail_repo_inverse
        try:
            try:
                rename.commit_rename(
                    prepared,
                    fault=lambda phase: (_ for _ in ()).throw(
                        rename.RenameError("stop after repository")
                    ) if phase == "repository-move" else None,
                )
            except rename.RenameError as exc:
                message = str(exc)
            else:
                raise AssertionError("expected rollback failure")
        finally:
            rename.os.replace = original_replace
        assert "rollback failed" in message
        assert str(new_repo) in message
        assert str(old_repo) not in message


def test_normal_verification_failure_rolls_back_the_entire_transaction():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        before = snapshot_tree(root)
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))

        def corrupt_before_verification(phase):
            if phase == "pin-refs":
                (roots.config / f"{NEW}.json").write_bytes(b"not JSON\n")

        expect_rename_error(
            lambda: rename.commit_rename(
                prepared, fault=corrupt_before_verification
            ),
            "invalid JSON",
        )
        assert snapshot_tree(root) == before
        assert staged_files(root) == []


def test_main_returns_usage_status_before_checking_privileges():
    original_geteuid = rename.os.geteuid
    stderr = io.StringIO()
    rename.os.geteuid = lambda: 1000
    try:
        with contextlib.redirect_stderr(stderr):
            status = rename.main([])
    finally:
        rename.os.geteuid = original_geteuid
    assert status == 2
    assert "usage:" in stderr.getvalue()
    assert "must run as root" not in stderr.getvalue()


def test_main_runs_the_transaction_under_both_sorted_exclusive_locks():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        rename.project_lock.provision_project_locks(roots.locks, [OLD])
        original_roots = rename.DEFAULT_ROOTS
        original_geteuid = rename.os.geteuid
        original_locks = rename.project_locks
        original_provision = rename.provision_project_locks
        original_hook_owner = rename._managed_hook_owner
        original_acl_uid = rename._kilnr_acl_uid
        original_production_policy = rename._production_policy
        observed = []

        def observed_provision(lock_root, names):
            observed.append(("provision", lock_root, tuple(names)))
            return original_provision(lock_root, names)

        def observed_locks(lock_root, names, *, exclusive):
            observed.append((lock_root, tuple(names), exclusive))
            return original_locks(lock_root, names, exclusive=exclusive)

        rename.DEFAULT_ROOTS = roots
        rename.os.geteuid = lambda: 0
        rename.project_locks = observed_locks
        rename.provision_project_locks = observed_provision
        rename._managed_hook_owner = lambda selected: (
            RENAME_PATH.stat().st_uid,
            RENAME_PATH.stat().st_gid,
        )
        rename._kilnr_acl_uid = lambda selected: None
        rename._production_policy = lambda selected: None
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                status = rename.main([OLD, NEW])
        finally:
            rename.DEFAULT_ROOTS = original_roots
            rename.os.geteuid = original_geteuid
            rename.project_locks = original_locks
            rename.provision_project_locks = original_provision
            rename._managed_hook_owner = original_hook_owner
            rename._kilnr_acl_uid = original_acl_uid
            rename._production_policy = original_production_policy

        assert status == 0
        assert observed == [
            ("provision", roots.locks, (NEW,)),
            (roots.locks, tuple(sorted((OLD, NEW))), True),
        ]
        output = stdout.getvalue()
        assert f"{OLD} -> {NEW}" in output
        assert str(roots.git / f"{NEW}.git") in output
        assert "git remote set-url" in output
        rename.verify_rename(roots, OLD, NEW, {OLD_ID: NEW_ID})


def test_root_run_git_commands_never_execute_repository_reference_hooks():
    for fail_after_verify in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots, repo, _, _ = make_fixture(root)
            marker = root / "reference-hook-ran"
            hook = repo / "hooks" / "reference-transaction"
            hook.write_text(
                "#!/bin/sh\nprintf 'executed\\n' >>" + shlex.quote(str(marker)) + "\n",
                encoding="utf-8",
            )
            os.chmod(hook, 0o755)
            prepared = rename.prepare_rename(
                rename.inventory_rename(roots, OLD, NEW)
            )

            if fail_after_verify:
                expect_rename_error(
                    lambda: rename.commit_rename(
                        prepared,
                        fault=lambda phase: (_ for _ in ()).throw(
                            rename.RenameError("force rollback")
                        ) if phase == "verify" else None,
                    ),
                    "force rollback",
                )
            else:
                rename.commit_rename(prepared)
            assert not marker.exists(), fail_after_verify


def test_verification_rejects_a_stale_old_allowlisted_build_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, build, _ = make_fixture(root)
        runtime_path = build / "runtime.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime["build_path"] = str(build)
        write_json(runtime_path, runtime)
        before = snapshot_tree(root)
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))

        def restore_stale_path(phase):
            if phase != "pin-refs":
                return
            installed = roots.state / "builds" / NEW_ID / "runtime.json"
            value = json.loads(installed.read_text(encoding="utf-8"))
            value["build_path"] = str(build)
            write_json(installed, value)
            os.chmod(installed, 0o640)

        expect_rename_error(
            lambda: rename.commit_rename(prepared, fault=restore_stale_path),
            "old managed path",
        )
        assert snapshot_tree(root) == before


def test_ref_rollback_failure_reports_the_repository_final_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        old_repo = roots.git / f"{OLD}.git"
        new_repo = roots.git / f"{NEW}.git"
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
        original_transaction = rename._git_update_ref_transaction
        calls = [0]

        def fail_ref_inverse(repository, commands):
            calls[0] += 1
            if calls[0] == 2:
                raise rename.RenameError("injected ref inverse failure")
            return original_transaction(repository, commands)

        rename._git_update_ref_transaction = fail_ref_inverse
        try:
            try:
                rename.commit_rename(
                    prepared,
                    fault=lambda phase: (_ for _ in ()).throw(
                        rename.RenameError("force rollback")
                    ) if phase == "verify" else None,
                )
            except rename.RenameError as exc:
                message = str(exc)
            else:
                raise AssertionError("expected ref rollback failure")
        finally:
            rename._git_update_ref_transaction = original_transaction

        assert old_repo.exists()
        assert not new_repo.exists()
        assert str(old_repo) in message
        assert str(new_repo) not in message


def test_metadata_rollback_failure_reports_the_backup_final_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        old_build = roots.state / "builds" / OLD_ID
        new_build = roots.state / "builds" / NEW_ID
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
        original_replace = rename.os.replace
        failed = [False]

        def fail_build_job_backup_inverse(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                not failed[0]
                and source_path.name.endswith(".rename-rollback")
                and destination_path == new_build / "job.json"
            ):
                failed[0] = True
                raise OSError("injected metadata inverse failure")
            return original_replace(source, destination)

        rename.os.replace = fail_build_job_backup_inverse
        try:
            try:
                rename.commit_rename(
                    prepared,
                    fault=lambda phase: (_ for _ in ()).throw(
                        rename.RenameError("force rollback")
                    ) if phase == "verify" else None,
                )
            except rename.RenameError as exc:
                message = str(exc)
            else:
                raise AssertionError("expected metadata rollback failure")
        finally:
            rename.os.replace = original_replace

        backups = list(old_build.glob("*.rename-rollback"))
        assert len(backups) == 1
        assert str(backups[0]) in message
        assert str(new_build) not in message


def test_packed_ref_commit_and_rollback_fsync_the_repository_directory():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, repo, _, _ = make_fixture(root)
        subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "pack-refs", "--all", "--prune"],
            check=True,
        )
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
        new_repo = roots.git / f"{NEW}.git"
        original_fsync = rename._fsync_directory
        fsynced = []

        def record_fsync(path):
            fsynced.append(Path(path))
            return original_fsync(path)

        rename._fsync_directory = record_fsync
        try:
            expect_rename_error(
                lambda: rename.commit_rename(
                    prepared,
                    fault=lambda phase: (_ for _ in ()).throw(
                        rename.RenameError("force rollback")
                    ) if phase == "verify" else None,
                ),
                "force rollback",
            )
        finally:
            rename._fsync_directory = original_fsync
        assert fsynced.count(new_repo) >= 2, fsynced


def test_every_repeated_phase_occurrence_rolls_back_exactly():
    phase_counts = {
        "build-move": 2,
        "metadata-backup": 9,
        "metadata-install": 9,
    }
    for phase, total in phase_counts.items():
        for target_occurrence in range(1, total + 1):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                roots, _, _, _ = make_fixture(root)
                make_build(roots, build_id=OLD_ID_2)
                before = snapshot_tree(root)
                prepared = rename.prepare_rename(
                    rename.inventory_rename(roots, OLD, NEW)
                )
                seen = [0]

                def fail_at_occurrence(current):
                    if current != phase:
                        return
                    seen[0] += 1
                    if seen[0] == target_occurrence:
                        raise rename.RenameError(
                            f"injected {phase} occurrence {target_occurrence}"
                        )

                expect_rename_error(
                    lambda: rename.commit_rename(
                        prepared, fault=fail_at_occurrence
                    ),
                    f"injected {phase} occurrence {target_occurrence}",
                )
                assert seen[0] == target_occurrence
                assert snapshot_tree(root) == before, (
                    phase,
                    target_occurrence,
                )


def test_mixed_loose_and_packed_ref_state_rolls_back_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, repo, _, _ = make_fixture(root)
        subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "pack-refs", "--all", "--prune"],
            check=True,
        )
        jobs = repo / "refs" / "kilnr" / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        os.chmod(repo / "refs" / "kilnr", 0o770)
        os.chmod(jobs, 0o770)
        loose = jobs / OLD_ID
        loose.write_text(SHA + "\n", encoding="ascii")
        os.chmod(loose, 0o640)
        before = snapshot_tree(root)
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))

        expect_rename_error(
            lambda: rename.commit_rename(
                prepared,
                fault=lambda phase: (_ for _ in ()).throw(
                    rename.RenameError("mixed ref rollback")
                ) if phase == "verify" else None,
            ),
            "mixed ref rollback",
        )
        assert snapshot_tree(root) == before


def test_packed_only_ref_with_absent_loose_namespace_rolls_back_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, repo, _, _ = make_fixture(root)
        subprocess.run(
            ["/usr/bin/git", f"--git-dir={repo}", "pack-refs", "--all", "--prune"],
            check=True,
        )
        jobs = repo / "refs" / "kilnr" / "jobs"
        kilnr_refs = jobs.parent
        for path in (jobs, kilnr_refs):
            if path.exists():
                assert not any(path.iterdir())
                path.rmdir()
        assert not jobs.exists()
        assert ref_value(repo, f"refs/kilnr/jobs/{OLD_ID}") == SHA
        before = snapshot_tree(root)
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))

        expect_rename_error(
            lambda: rename.commit_rename(
                prepared,
                fault=lambda phase: (_ for _ in ()).throw(
                    rename.RenameError("packed absent namespace rollback")
                ) if phase == "verify" else None,
            ),
            "packed absent namespace rollback",
        )
        assert snapshot_tree(root) == before


def test_absent_ref_and_namespace_state_rolls_back_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, repo, _, _ = make_fixture(root)
        jobs = repo / "refs" / "kilnr" / "jobs"
        (jobs / OLD_ID).unlink()
        jobs.rmdir()
        (repo / "refs" / "kilnr").rmdir()
        before = snapshot_tree(root)
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))

        expect_rename_error(
            lambda: rename.commit_rename(
                prepared,
                fault=lambda phase: (_ for _ in ()).throw(
                    rename.RenameError("absent ref rollback")
                ) if phase == "verify" else None,
            ),
            "absent ref rollback",
        )
        assert snapshot_tree(root) == before


def test_ref_security_restoration_failure_rolls_back_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        before = snapshot_tree(root)
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
        new_ref = (
            roots.git
            / f"{NEW}.git"
            / "refs"
            / "kilnr"
            / "jobs"
            / NEW_ID
        )
        new_repo = roots.git / f"{NEW}.git"
        original_restore = rename._restore_file_facts
        original_fsync = rename._fsync_directory
        failed = [False]
        events = []

        def fail_new_ref_security(path, facts):
            if Path(path) == new_ref and not failed[0]:
                failed[0] = True
                events.append("security")
                raise OSError("injected ref security failure")
            return original_restore(path, facts)

        def record_repository_fsync(path):
            if Path(path) == new_repo:
                events.append("repository-fsync")
            return original_fsync(path)

        rename._restore_file_facts = fail_new_ref_security
        rename._fsync_directory = record_repository_fsync
        try:
            expect_rename_error(
                lambda: rename.commit_rename(prepared),
                "injected ref security failure",
            )
        finally:
            rename._restore_file_facts = original_restore
            rename._fsync_directory = original_fsync
        assert events[:2] == ["repository-fsync", "security"]
        assert snapshot_tree(root) == before


def test_ref_repository_fsync_failure_rolls_back_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        before = snapshot_tree(root)
        prepared = rename.prepare_rename(rename.inventory_rename(roots, OLD, NEW))
        new_repo = roots.git / f"{NEW}.git"
        original_fsync = rename._fsync_directory
        failed = [False]

        def fail_first_repository_fsync(path):
            if Path(path) == new_repo and not failed[0]:
                failed[0] = True
                raise OSError("injected repository fsync failure")
            return original_fsync(path)

        rename._fsync_directory = fail_first_repository_fsync
        try:
            expect_rename_error(
                lambda: rename.commit_rename(prepared),
                "injected repository fsync failure",
            )
        finally:
            rename._fsync_directory = original_fsync
        assert snapshot_tree(root) == before


def test_hardened_git_boundary_disables_hooks_and_sanitizes_configuration():
    repository = Path("/srv/git/example.git")
    command = rename._hardened_git_command(repository, "update-ref", "--stdin")
    assert command[:5] == [
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={repository}",
    ]
    assert command[5:] == [f"--git-dir={repository}", "update-ref", "--stdin"]

    poisoned = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/tmp/attacker-hooks",
        "LD_PRELOAD": "/tmp/attacker-library.so",
    }
    original = {key: os.environ.get(key) for key in poisoned}
    os.environ.update(poisoned)
    try:
        environment = rename._hardened_git_environment()
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert not any(key.startswith("GIT_CONFIG_KEY_") for key in environment)
    assert not any(key.startswith("GIT_CONFIG_VALUE_") for key in environment)
    assert "LD_PRELOAD" not in environment
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_main_returns_one_without_traceback_for_an_operational_failure():
    with tempfile.TemporaryDirectory() as tmp:
        roots = make_roots(Path(tmp))
        original_roots = rename.DEFAULT_ROOTS
        original_geteuid = rename.os.geteuid
        rename.DEFAULT_ROOTS = roots
        rename.os.geteuid = lambda: 0
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = rename.main([OLD, NEW])
        finally:
            rename.DEFAULT_ROOTS = original_roots
            rename.os.geteuid = original_geteuid
        assert status == 1
        assert stdout.getvalue() == ""
        assert stderr.getvalue().startswith("kilnr project rename: ")
        assert "Traceback" not in stderr.getvalue()


def mutator_race_expected_state(roots, script_name, project):
    if script_name == "project-webhook-set":
        return (
            roots.secrets / f"{project}.discord-webhook"
        ).read_text(encoding="utf-8") == (
            "https://discord.com/api/webhooks/123/race-token\n"
        )
    value = roots.secrets / project / "TOKEN.value"
    metadata = roots.secrets / project / "TOKEN.json"
    if script_name == "secret-delete":
        return not value.exists() and not metadata.exists()
    expected = b"race text" if script_name == "secret-set" else b"race file"
    expected_kind = "text" if script_name == "secret-set" else "file"
    return (
        value.read_bytes() == expected
        and json.loads(metadata.read_text(encoding="utf-8"))["kind"] == expected_kind
    )


def run_mutator_rename_race(script_name, *, rollback):
    context = multiprocessing.get_context("fork")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roots, _, _, _ = make_fixture(root)
        rename.provision_project_locks(roots.locks, [OLD, NEW])
        roots.locks.chmod(0o550)
        source = root / "race-secret"
        source.write_bytes(b"race file")
        mutator_parent, mutator_child = context.Pipe()
        rename_parent, rename_child = context.Pipe()

        def run_mutator():
            module = load_python(
                ROOT / "libexec" / script_name,
                f"{script_name.replace('-', '_')}_rename_race",
            )
            module.CONFIG_ROOT = roots.config
            module.SECRETS_ROOT = roots.secrets
            module.PROJECT_LOCK_ROOT = roots.locks
            module.os.geteuid = lambda: 0
            if hasattr(module, "grp"):
                module.grp.getgrnam = lambda _name: type(
                    "Group", (), {"gr_gid": os.getgid()}
                )()

            if script_name == "project-webhook-set":
                original_writer = module.tempfile.mkstemp

                def synchronized_writer(*args, **kwargs):
                    mutator_child.send("writer-ready")
                    assert mutator_child.recv() == "continue"
                    return original_writer(*args, **kwargs)

                module.tempfile.mkstemp = synchronized_writer
                module.os.fchown = lambda fd, _uid, _gid: None
                sys.stdin = io.StringIO(
                    "https://discord.com/api/webhooks/123/race-token\n"
                )
                argv = [script_name, OLD]
            elif script_name in ("secret-set", "secret-set-file"):
                original_writer = module.secrets.store_secret

                def synchronized_writer(*args, **kwargs):
                    mutator_child.send("writer-ready")
                    assert mutator_child.recv() == "continue"
                    kwargs["uid"] = os.getuid()
                    kwargs["gid"] = os.getgid()
                    return original_writer(*args, **kwargs)

                module.secrets.store_secret = synchronized_writer
                if script_name == "secret-set":
                    sys.stdin = io.TextIOWrapper(io.BytesIO(b"race text"))
                    argv = [script_name, OLD, "TOKEN"]
                else:
                    argv = [script_name, OLD, "TOKEN", str(source)]
            else:
                original_writer = module.secrets.delete_secret

                def synchronized_writer(*args, **kwargs):
                    mutator_child.send("writer-ready")
                    assert mutator_child.recv() == "continue"
                    return original_writer(*args, **kwargs)

                module.secrets.delete_secret = synchronized_writer
                argv = [script_name, OLD, "TOKEN"]

            sys.argv = argv
            try:
                mutator_child.send(("done", module.main()))
            except BaseException as exc:
                mutator_child.send(("error", repr(exc)))
            finally:
                mutator_child.close()

        def run_rename():
            local = load_python(RENAME_PATH, f"rename_race_{script_name}_{rollback}")
            local_roots = local.Roots(
                git=roots.git,
                config=roots.config,
                secrets=roots.secrets,
                state=roots.state,
                locks=roots.locks,
            )
            rename_child.send("lock-attempt")
            try:
                with local.project_locks(
                    local_roots.locks, [OLD, NEW], exclusive=True
                ):
                    prepared = local.prepare_rename(
                        local.inventory_rename(local_roots, OLD, NEW)
                    )

                    def fail_verification(phase):
                        if rollback and phase == "verify":
                            raise local.RenameError("forced rollback race")

                    local.commit_rename(prepared, fault=fail_verification)
            except local.RenameError as exc:
                rename_child.send(("rollback", str(exc)))
            except BaseException as exc:
                rename_child.send(("error", repr(exc)))
            else:
                rename_child.send(("success", None))
            finally:
                rename_child.close()

        mutator_process = context.Process(target=run_mutator)
        mutator_process.start()
        mutator_child.close()
        assert mutator_parent.poll(5), "mutator did not reach durable writer"
        assert mutator_parent.recv() == "writer-ready"

        rename_process = context.Process(target=run_rename)
        rename_process.start()
        rename_child.close()
        assert rename_parent.poll(5), "rename did not attempt its exclusive locks"
        assert rename_parent.recv() == "lock-attempt"
        assert not rename_parent.poll(0.2), "rename entered while mutator held shared lock"

        mutator_parent.send("continue")
        assert mutator_parent.poll(5), "mutator did not finish"
        assert mutator_parent.recv() == ("done", 0)
        mutator_process.join(timeout=5)
        assert mutator_process.exitcode == 0
        mutator_parent.close()

        assert rename_parent.poll(10), "rename did not finish after mutator released lock"
        outcome, detail = rename_parent.recv()
        rename_process.join(timeout=10)
        assert rename_process.exitcode == 0, (outcome, detail)
        rename_parent.close()
        if rollback:
            assert outcome == "rollback", detail
            assert "forced rollback race" in detail
            assert mutator_race_expected_state(roots, script_name, OLD)
            assert not (roots.config / f"{NEW}.json").exists()
        else:
            assert outcome == "success", detail
            assert mutator_race_expected_state(roots, script_name, NEW)
            assert not (roots.config / f"{OLD}.json").exists()


def test_all_project_mutators_serialize_before_successful_rename():
    for script_name in (
        "project-webhook-set",
        "secret-set",
        "secret-set-file",
        "secret-delete",
    ):
        run_mutator_rename_race(script_name, rollback=False)


def test_all_project_mutators_serialize_before_forced_rename_rollback():
    for script_name in (
        "project-webhook-set",
        "secret-set",
        "secret-set-file",
        "secret-delete",
    ):
        run_mutator_rename_race(script_name, rollback=True)


def main():
    tests = [
        test_inventory_maps_hyphenated_build_identity_without_mutating_state,
        test_inventory_ignores_unrelated_builds_and_active_jobs,
        test_inventory_rejects_source_jobs_in_both_active_queues,
        test_inventory_rejects_invalid_names_before_path_construction,
        test_inventory_rejects_every_fixed_destination_collision,
        test_inventory_rejects_a_cross_filesystem_source_move,
        test_inventory_rejects_dangling_destination_symlinks,
        test_inventory_rejects_a_destination_pin_ref_collision,
        test_inventory_maps_a_source_pin_ref_stored_in_packed_refs,
        test_inventory_maps_packed_refs_when_the_loose_namespace_is_absent,
        test_inventory_rejects_a_packed_destination_when_loose_namespace_is_absent,
        test_inventory_rejects_stale_and_ambiguous_managed_refs,
        test_inventory_type_checks_every_loose_managed_ref_entry,
        test_inventory_rejects_a_pin_ref_that_points_to_the_wrong_sha,
        test_inventory_rejects_symlinks_in_managed_locations,
        test_inventory_rejects_unexpected_fifo_secret_entries,
        test_inventory_rejects_unexpected_active_queue_entry_types,
        test_inventory_rejects_a_symlinked_managed_root,
        test_inventory_rejects_a_symlinked_queue_root,
        test_inventory_rejects_a_symlinked_refs_kilnr_component,
        test_inventory_rejects_malformed_managed_json,
        test_inventory_rejects_inconsistent_repository_config_and_hook,
        test_inventory_applies_strict_project_config_validation,
        test_inventory_rejects_inconsistent_managed_build_schemas,
        test_terminal_selection_and_pre_runtime_failures_rename_successfully,
        test_terminal_selection_and_pre_runtime_failures_roll_back_exactly,
        test_inventory_validates_managed_build_top_level_entry_types,
        test_inventory_keeps_commands_and_runtime_payloads_opaque,
        test_inventory_rejects_invalid_secret_names,
        test_inventory_rejects_unsafe_managed_modes,
        test_inventory_accepts_project_create_acl_mask_and_execute_cache_parent_modes,
        test_inventory_validates_project_create_named_user_acl_policy,
        test_inventory_accepts_producer_authentic_loose_ref_acl_policy,
        test_git_created_loose_ref_inherits_authentic_acl_on_linux,
        test_real_linux_packed_only_ref_acl_commit_and_exact_rollback,
        test_inventory_rejects_extra_named_user_in_each_managed_ref_acl,
        test_inventory_rejects_extra_named_group_in_each_managed_ref_acl,
        test_inventory_rejects_writable_owning_group_in_each_managed_ref_acl,
        test_inventory_rejects_untrusted_managed_hook_owner,
        test_production_managed_hook_policy_requires_root_root,
        test_inventory_rejects_inconsistent_managed_ownership,
        test_production_repository_policy_rejects_a_kilnr_owned_repository,
        test_production_repository_policy_rejects_kilnr_writable_refs_heads,
        test_production_metadata_policy_rejects_extra_acl_writers,
        test_production_root_policy_rejects_wrong_owner_group_and_mode,
        test_inventory_captures_acl_facts_for_managed_sources,
        test_inventory_rejects_build_ids_not_anchored_to_structured_metadata,
        test_prepare_rewrites_only_allowlisted_metadata_and_preserves_attributes,
        test_prepare_cleans_every_temp_after_injected_failures,
        test_prepared_cleanup_removes_files_before_and_after_a_build_parent_move,
        test_prepare_applies_fchown_before_the_final_fchmod,
        test_prepare_refuses_permission_drift_after_inventory,
        test_prepare_copies_captured_acl_data_to_staged_files,
        test_prepare_rejects_pipeline_entries_with_an_unexpected_build_id,
        test_commit_renames_all_managed_state_and_preserves_opaque_payloads,
        test_packed_only_commit_materializes_the_fixture_loose_ref_policy,
        test_packed_only_commit_materializes_the_production_loose_ref_policy,
        test_commit_rolls_back_exactly_after_every_stable_phase,
        test_commit_rolls_back_a_packed_source_ref_with_update_ref,
        test_packed_only_production_policy_rolls_back_exactly_after_verification,
        test_commit_reports_failed_rollback_with_both_exact_paths,
        test_normal_verification_failure_rolls_back_the_entire_transaction,
        test_main_returns_usage_status_before_checking_privileges,
        test_main_runs_the_transaction_under_both_sorted_exclusive_locks,
        test_root_run_git_commands_never_execute_repository_reference_hooks,
        test_verification_rejects_a_stale_old_allowlisted_build_path,
        test_ref_rollback_failure_reports_the_repository_final_path,
        test_metadata_rollback_failure_reports_the_backup_final_path,
        test_packed_ref_commit_and_rollback_fsync_the_repository_directory,
        test_every_repeated_phase_occurrence_rolls_back_exactly,
        test_mixed_loose_and_packed_ref_state_rolls_back_exactly,
        test_packed_only_ref_with_absent_loose_namespace_rolls_back_exactly,
        test_absent_ref_and_namespace_state_rolls_back_exactly,
        test_ref_security_restoration_failure_rolls_back_exactly,
        test_ref_repository_fsync_failure_rolls_back_exactly,
        test_hardened_git_boundary_disables_hooks_and_sanitizes_configuration,
        test_main_returns_one_without_traceback_for_an_operational_failure,
        test_all_project_mutators_serialize_before_successful_rename,
        test_all_project_mutators_serialize_before_forced_rename_rollback,
    ]
    for test in tests:
        test()
        print(f"OK project rename: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
