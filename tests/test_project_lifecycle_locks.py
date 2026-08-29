#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import io
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIBEXEC = ROOT / "libexec"
LOCKS_PATH = LIBEXEC / "kilnr_project_lock.py"


def load_script(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


sys.path.insert(0, str(LIBEXEC))
locks = load_script(LOCKS_PATH, "kilnr_project_lock_lifecycle_test")


def provision(root: Path, *projects: str):
    locks.provision_project_locks(root, projects)


def receive(connection):
    assert connection.poll(5), "child did not reach synchronization point"
    return connection.recv()


def assert_lock_busy(root: Path, project: str, *, exclusive: bool):
    try:
        with locks.project_locks(
            root,
            [project],
            exclusive=exclusive,
            blocking=False,
        ):
            pass
    except locks.ProjectLockBusy:
        return
    raise AssertionError("lifecycle lock was released before the operation completed")


def assert_lock_available(root: Path, project: str, *, exclusive: bool):
    with locks.project_locks(
        root,
        [project],
        exclusive=exclusive,
        blocking=False,
    ):
        pass


def finish_process(process, connection):
    process.join(timeout=5)
    connection.close()
    assert process.exitcode == 0, f"child did not complete: {process.exitcode}"


def test_enqueue_shared_lock_spans_config_load_and_atomic_publication():
    context = multiprocessing.get_context("fork")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        lock_root = root / "locks"
        lock_root.mkdir()
        provision(lock_root, "demo")
        repo = root / "demo.git"
        repo.mkdir()
        parent, child = context.Pipe()

        def run_enqueue():
            enqueue = load_script(LIBEXEC / "enqueue", "enqueue_lifecycle_success")
            enqueue.PROJECT_LOCK_ROOT = lock_root

            def load_project(project):
                child.send("config")
                assert child.recv() == "continue"
                return {
                    "schema": 1,
                    "project": project,
                    "repository": str(repo),
                }

            def git(_repo, *args):
                if args[:2] == ("rev-parse", "--verify"):
                    return "b" * 40
                return ""

            def atomic_publish(_job_id, _job):
                child.send("publish")
                assert child.recv() == "continue"

            enqueue.load_project = load_project
            enqueue.git = git
            enqueue.atomic_publish = atomic_publish
            sys.argv = [
                "enqueue",
                "demo",
                "a" * 40,
                "b" * 40,
                "refs/heads/main",
            ]
            try:
                child.send(("done", enqueue.main()))
            except BaseException as exc:
                child.send(("error", repr(exc)))
            finally:
                child.close()

        process = context.Process(target=run_enqueue)
        process.start()
        child.close()
        assert receive(parent) == "config"
        assert_lock_available(lock_root, "demo", exclusive=False)
        assert_lock_busy(lock_root, "demo", exclusive=True)
        parent.send("continue")
        assert receive(parent) == "publish"
        assert_lock_busy(lock_root, "demo", exclusive=True)
        parent.send("continue")
        assert receive(parent) == ("done", 0)
        finish_process(process, parent)
        assert_lock_available(lock_root, "demo", exclusive=True)


def test_enqueue_shared_lock_spans_failed_pin_cleanup():
    context = multiprocessing.get_context("fork")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lock_root = root / "locks"
        lock_root.mkdir()
        provision(lock_root, "demo")
        repo = root / "demo.git"
        repo.mkdir()
        parent, child = context.Pipe()

        def run_enqueue():
            enqueue = load_script(LIBEXEC / "enqueue", "enqueue_lifecycle_cleanup")
            enqueue.PROJECT_LOCK_ROOT = lock_root
            enqueue.load_project = lambda project: {
                "schema": 1,
                "project": project,
                "repository": str(repo),
            }
            enqueue.git = lambda _repo, *args: "b" * 40 if args[0] == "rev-parse" else ""

            def fail_publish(_job_id, _job):
                raise RuntimeError("publication failed")

            def cleanup_pin(argv, **kwargs):
                child.send(("cleanup", argv, kwargs))
                assert child.recv() == "continue"
                return subprocess.CompletedProcess(argv, 0)

            enqueue.atomic_publish = fail_publish
            enqueue.subprocess.run = cleanup_pin
            sys.argv = [
                "enqueue",
                "demo",
                "a" * 40,
                "b" * 40,
                "refs/heads/main",
            ]
            try:
                enqueue.main()
            except RuntimeError as exc:
                child.send(("done", str(exc)))
            except BaseException as exc:
                child.send(("error", repr(exc)))
            finally:
                child.close()

        process = context.Process(target=run_enqueue)
        process.start()
        child.close()
        message = receive(parent)
        assert message[0] == "cleanup"
        assert message[1][-3:-1] == ["update-ref", "-d"]
        assert_lock_busy(lock_root, "demo", exclusive=True)
        parent.send("continue")
        assert receive(parent) == ("done", "publication failed")
        finish_process(process, parent)
        assert_lock_available(lock_root, "demo", exclusive=True)


def test_delete_exclusive_lock_spans_config_validation_and_deletion():
    context = multiprocessing.get_context("fork")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lock_root = root / "locks"
        lock_root.mkdir()
        provision(lock_root, "demo")
        git_root = root / "git"
        config_root = root / "projects"
        secrets_root = root / "secrets"
        git_root.mkdir()
        config_root.mkdir()
        secrets_root.mkdir()
        repo = git_root / "demo.git"
        repo.mkdir()
        for name in ("HEAD", "objects", "refs"):
            path = repo / name
            path.mkdir() if name != "HEAD" else path.write_text("ref: refs/heads/main\n")
        config_path = config_root / "demo.json"
        config_path.write_text("{}\n", encoding="utf-8")
        secret_path = secrets_root / "demo.discord-webhook"
        secret_path.write_text("https://example.invalid/webhook\n", encoding="utf-8")
        secret_dir = secrets_root / "demo"
        secret_dir.mkdir()
        (secret_dir / "TOKEN").write_text("secret\n", encoding="utf-8")
        parent, child = context.Pipe()

        def run_delete():
            project_delete = load_script(LIBEXEC / "project-delete", "project_delete_lifecycle")
            project_delete.PROJECT_LOCK_ROOT = lock_root
            project_delete.GIT_ROOT = git_root
            project_delete.CONFIG_ROOT = config_root
            project_delete.SECRETS_ROOT = secrets_root
            project_delete.QUEUE_DIRS = ()
            project_delete.os.geteuid = lambda: 0
            original_rmtree = project_delete.shutil.rmtree

            def read_json(_path):
                child.send("config")
                assert child.recv() == "continue"
                return {
                    "schema": 1,
                    "project": "demo",
                    "repository": str(repo),
                    "discord": {"webhook_file": str(secret_path)},
                }

            def remove_tree(path):
                if Path(path) == repo:
                    child.send("delete")
                    assert child.recv() == "continue"
                elif Path(path) == secret_dir:
                    child.send("final-cleanup")
                    assert child.recv() == "continue"
                original_rmtree(path)

            project_delete.read_json = read_json
            project_delete.shutil.rmtree = remove_tree
            sys.argv = ["project-delete", "demo"]
            try:
                child.send(("done", project_delete.main()))
            except BaseException as exc:
                child.send(("error", repr(exc)))
            finally:
                child.close()

        process = context.Process(target=run_delete)
        process.start()
        child.close()
        assert receive(parent) == "config"
        assert_lock_busy(lock_root, "demo", exclusive=False)
        parent.send("continue")
        assert receive(parent) == "delete"
        assert_lock_busy(lock_root, "demo", exclusive=False)
        parent.send("continue")
        assert receive(parent) == "final-cleanup"
        assert not config_path.exists()
        assert not secret_path.exists()
        assert_lock_busy(lock_root, "demo", exclusive=False)
        parent.send("continue")
        assert receive(parent) == ("done", 0)
        finish_process(process, parent)
        assert_lock_available(lock_root, "demo", exclusive=False)
        assert not repo.exists()
        assert not config_path.exists()
        assert not secret_path.exists()
        assert not secret_dir.exists()


def test_project_lock_run_executes_exact_argv_under_exclusive_lock():
    context = multiprocessing.get_context("fork")
    with tempfile.TemporaryDirectory() as tmp:
        lock_root = Path(tmp) / "locks"
        lock_root.mkdir()
        parent, child = context.Pipe()

        def run_wrapper():
            wrapper = load_script(LIBEXEC / "project-lock-run", "project_lock_run_test")
            wrapper.PROJECT_LOCK_ROOT = lock_root
            wrapper.os.geteuid = lambda: 0

            def run(argv, **kwargs):
                child.send(("run", argv, kwargs))
                assert child.recv() == "continue"
                return subprocess.CompletedProcess(argv, 17)

            wrapper.subprocess.run = run
            sys.argv = [
                "project-lock-run",
                "--exclusive",
                "demo",
                "--",
                "/program with spaces",
                "literal;argument",
            ]
            try:
                child.send(("done", wrapper.main()))
            except BaseException as exc:
                child.send(("error", repr(exc)))
            finally:
                child.close()

        process = context.Process(target=run_wrapper)
        process.start()
        child.close()
        message = receive(parent)
        assert message == (
            "run",
            ["/program with spaces", "literal;argument"],
            {"check": False},
        )
        assert_lock_busy(lock_root, "demo", exclusive=False)
        parent.send("continue")
        assert receive(parent) == ("done", 17)
        finish_process(process, parent)
        assert_lock_available(lock_root, "demo", exclusive=False)


def test_project_lock_run_normalizes_signal_exit_status():
    with tempfile.TemporaryDirectory() as tmp:
        wrapper = load_script(LIBEXEC / "project-lock-run", "project_lock_run_signal")
        wrapper.PROJECT_LOCK_ROOT = Path(tmp)
        wrapper.os.geteuid = lambda: 0
        original_run = subprocess.run
        original_argv = sys.argv
        try:
            wrapper.subprocess.run = lambda argv, **kwargs: subprocess.CompletedProcess(
                argv,
                -signal.SIGTERM,
            )
            sys.argv = [
                "project-lock-run",
                "--exclusive",
                "demo",
                "--",
                "/program",
            ]

            assert wrapper.main() == 128 + signal.SIGTERM
        finally:
            subprocess.run = original_run
            sys.argv = original_argv


def test_create_cli_dispatches_through_project_lock_wrapper():
    cli = load_script(ROOT / "bin" / "kilnr", "kilnr_create_lock_dispatch")
    calls = []
    cli.privileged_command = lambda helper, *args, **kwargs: calls.append(
        (helper, args, kwargs)
    ) or 0
    sys.argv = ["kilnr", "project", "create", "demo"]

    assert cli.main() == 0
    assert calls == [
        (
            cli.PROJECT_LOCK_RUN,
            ("--exclusive", "demo", "--", cli.PROJECT_CREATE, "demo"),
            {},
        )
    ]


def run_actual_receive_across_repository_rename(*, rollback: bool):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        git_root = root / "git"
        config_root = root / "projects"
        lock_root = root / "locks"
        queue_tmp = root / "queue" / "tmp"
        queue_incoming = root / "queue" / "incoming"
        installed = root / "installed"
        hooks = installed / "git-hooks"
        for path in (
            git_root,
            config_root,
            lock_root,
            queue_tmp,
            queue_incoming,
            hooks,
        ):
            path.mkdir(parents=True, exist_ok=True)
        provision(lock_root, "old", "new")
        lock_root.chmod(0o550)

        installed_hook = hooks / "post-receive"
        shutil.copy2(LIBEXEC / "git-hooks" / "post-receive", installed_hook)
        installed_hook.chmod(0o755)
        ready = root / "enqueue-ready"
        resume = root / "enqueue-resume"
        wrapper = installed / "enqueue"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import importlib.machinery, importlib.util, json, sys, time\n"
            "from pathlib import Path\n"
            f"Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
            f"while not Path({str(resume)!r}).exists(): time.sleep(0.01)\n"
            f"source = Path({str(LIBEXEC / 'enqueue')!r})\n"
            "loader = importlib.machinery.SourceFileLoader('receive_enqueue', str(source))\n"
            "spec = importlib.util.spec_from_loader('receive_enqueue', loader)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "loader.exec_module(module)\n"
            f"module.GIT_ROOT = Path({str(git_root)!r})\n"
            f"module.CONFIG_ROOT = Path({str(config_root)!r})\n"
            f"module.PROJECT_LOCK_ROOT = Path({str(lock_root)!r})\n"
            f"module.QUEUE_TMP = Path({str(queue_tmp)!r})\n"
            f"module.QUEUE_INCOMING = Path({str(queue_incoming)!r})\n"
            "def load_project(project):\n"
            "    path = module.CONFIG_ROOT / f'{project}.json'\n"
            "    value = json.loads(path.read_text(encoding='utf-8'))\n"
            "    if value.get('project') != project:\n"
            "        raise module.KilnrError('project mismatch')\n"
            "    return value\n"
            "module.load_project = load_project\n"
            "raise SystemExit(module.main())\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        old_repo = git_root / "old.git"
        new_repo = git_root / "new.git"
        subprocess.run(
            ["/usr/bin/git", "init", "--bare", "--initial-branch=main", str(old_repo)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        (old_repo / "hooks" / "post-receive").symlink_to(installed_hook)
        old_config = config_root / "old.json"
        new_config = config_root / "new.json"
        old_config.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "project": "old",
                    "repository": str(old_repo),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        work = root / "work"
        subprocess.run(
            ["/usr/bin/git", "init", "--initial-branch=main", str(work)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(["/usr/bin/git", "-C", str(work), "config", "user.name", "Kilnr Test"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(work), "config", "user.email", "kilnr@example.invalid"], check=True)
        (work / "README").write_text("receive race\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "-C", str(work), "add", "README"], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", str(work), "commit", "-m", "receive race"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        push = subprocess.Popen(
            [
                "/usr/bin/git",
                "-C",
                str(work),
                "push",
                str(old_repo),
                "HEAD:refs/heads/main",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            stdout, stderr = push.communicate(timeout=5)
            raise AssertionError(
                "managed post-receive did not invoke its sibling enqueue: "
                f"stdout={stdout!r}, stderr={stderr!r}"
            )

        with locks.project_locks(lock_root, ["old", "new"], exclusive=True):
            os.replace(old_repo, new_repo)
            config = json.loads(old_config.read_text(encoding="utf-8"))
            config.update(project="new", repository=str(new_repo))
            new_config.write_text(json.dumps(config) + "\n", encoding="utf-8")
            old_config.unlink()
            if rollback:
                os.replace(new_repo, old_repo)
                config.update(project="old", repository=str(old_repo))
                old_config.write_text(json.dumps(config) + "\n", encoding="utf-8")
                new_config.unlink()

        resume.write_text("continue", encoding="utf-8")
        stdout, stderr = push.communicate(timeout=10)
        assert push.returncode == 0, (stdout, stderr)
        queued = list(queue_incoming.glob("*.json"))
        assert len(queued) == 1, (stdout, stderr, queued)
        job = json.loads(queued[0].read_text(encoding="utf-8"))
        expected_project = "old" if rollback else "new"
        expected_repo = old_repo if rollback else new_repo
        assert job["project"] == expected_project
        assert expected_project in job["id"]
        assert job["sha"] == subprocess.run(
            ["/usr/bin/git", f"--git-dir={expected_repo}", "rev-parse", "refs/heads/main"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()


def test_actual_receive_resolves_new_identity_after_successful_rename():
    run_actual_receive_across_repository_rename(rollback=False)


def test_actual_receive_resolves_restored_identity_after_rename_rollback():
    run_actual_receive_across_repository_rename(rollback=True)


def assert_mutator_shared_lock_spans_validation_and_durable_write(script_name: str):
    context = multiprocessing.get_context("fork")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config_root = root / "projects"
        secrets_root = root / "secrets"
        lock_root = root / "locks"
        for path in (config_root, secrets_root, lock_root, secrets_root / "demo"):
            path.mkdir(parents=True, exist_ok=True)
        provision(lock_root, "demo")
        lock_root.chmod(0o550)
        webhook = secrets_root / "demo.discord-webhook"
        config = {
            "schema": 1,
            "project": "demo",
            "repository": str(root / "demo.git"),
            "discord": {"webhook_file": str(webhook)},
        }
        (config_root / "demo.json").write_text(
            json.dumps(config) + "\n", encoding="utf-8"
        )
        source = root / "secret-source"
        source.write_bytes(b"file secret")
        (secrets_root / "demo" / "TOKEN.value").write_bytes(b"old secret")
        (secrets_root / "demo" / "TOKEN.json").write_text(
            '{"schema": 1, "scope": "release", "kind": "text"}\n',
            encoding="utf-8",
        )
        parent, child = context.Pipe()

        def run_mutator():
            module = load_script(LIBEXEC / script_name, f"{script_name}_lock_span")
            module.CONFIG_ROOT = config_root
            module.SECRETS_ROOT = secrets_root
            module.PROJECT_LOCK_ROOT = lock_root
            module.os.geteuid = lambda: 0
            if hasattr(module, "grp"):
                module.grp.getgrnam = lambda _name: type("Group", (), {"gr_gid": os.getgid()})()

            if script_name == "project-webhook-set":
                original_writer = module.tempfile.mkstemp

                def synchronized_writer(*args, **kwargs):
                    child.send("write")
                    assert child.recv() == "continue"
                    return original_writer(*args, **kwargs)

                module.tempfile.mkstemp = synchronized_writer
                module.os.fchown = lambda fd, _uid, _gid: None
                sys.stdin = io.StringIO(
                    "https://discord.com/api/webhooks/123/token-value\n"
                )
                argv = [script_name, "demo"]
            elif script_name in ("secret-set", "secret-set-file"):
                original_writer = module.secrets.store_secret

                def synchronized_writer(*args, **kwargs):
                    child.send("write")
                    assert child.recv() == "continue"
                    kwargs["uid"] = os.getuid()
                    kwargs["gid"] = os.getgid()
                    return original_writer(*args, **kwargs)

                module.secrets.store_secret = synchronized_writer
                if script_name == "secret-set":
                    sys.stdin = io.TextIOWrapper(io.BytesIO(b"new secret"))
                    argv = [script_name, "demo", "TOKEN"]
                else:
                    argv = [script_name, "demo", "TOKEN", str(source)]
            else:
                original_writer = module.secrets.delete_secret

                def synchronized_writer(*args, **kwargs):
                    child.send("write")
                    assert child.recv() == "continue"
                    return original_writer(*args, **kwargs)

                module.secrets.delete_secret = synchronized_writer
                argv = [script_name, "demo", "TOKEN"]

            sys.argv = argv
            try:
                child.send(("done", module.main()))
            except BaseException as exc:
                child.send(("error", repr(exc)))
            finally:
                child.close()

        process = context.Process(target=run_mutator)
        process.start()
        child.close()
        assert receive(parent) == "write"
        probe_error = None
        try:
            assert_lock_busy(lock_root, "demo", exclusive=True)
        except AssertionError as exc:
            probe_error = exc
        parent.send("continue")
        assert receive(parent) == ("done", 0)
        finish_process(process, parent)
        if probe_error is not None:
            raise probe_error
        assert_lock_available(lock_root, "demo", exclusive=True)


def test_webhook_mutation_holds_shared_lock_through_durable_write():
    assert_mutator_shared_lock_spans_validation_and_durable_write(
        "project-webhook-set"
    )


def test_text_secret_mutation_holds_shared_lock_through_durable_write():
    assert_mutator_shared_lock_spans_validation_and_durable_write("secret-set")


def test_file_secret_mutation_holds_shared_lock_through_durable_write():
    assert_mutator_shared_lock_spans_validation_and_durable_write("secret-set-file")


def test_secret_delete_holds_shared_lock_through_durable_write():
    assert_mutator_shared_lock_spans_validation_and_durable_write("secret-delete")


def test_lifecycle_lock_helpers_are_installed():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "project-lock-run" in install
    assert "kilnr_project_lock.py" in install


def main():
    tests = [
        test_enqueue_shared_lock_spans_config_load_and_atomic_publication,
        test_enqueue_shared_lock_spans_failed_pin_cleanup,
        test_delete_exclusive_lock_spans_config_validation_and_deletion,
        test_project_lock_run_executes_exact_argv_under_exclusive_lock,
        test_project_lock_run_normalizes_signal_exit_status,
        test_create_cli_dispatches_through_project_lock_wrapper,
        test_actual_receive_resolves_new_identity_after_successful_rename,
        test_actual_receive_resolves_restored_identity_after_rename_rollback,
        test_webhook_mutation_holds_shared_lock_through_durable_write,
        test_text_secret_mutation_holds_shared_lock_through_durable_write,
        test_file_secret_mutation_holds_shared_lock_through_durable_write,
        test_secret_delete_holds_shared_lock_through_durable_write,
        test_lifecycle_lock_helpers_are_installed,
    ]
    for test in tests:
        test()
        print(f"OK project lifecycle lock: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
