#!/usr/bin/env python3
import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PYTHON_FILES = [
    ROOT / "bin" / "kilnr",
    ROOT / "libexec" / "controller",
    ROOT / "libexec" / "pipeline.py",
    ROOT / "libexec" / "artifacts.py",
    ROOT / "libexec" / "kilnr_secrets.py",
    ROOT / "libexec" / "enqueue",
    ROOT / "libexec" / "execute",
    ROOT / "libexec" / "notify-discord",
    ROOT / "libexec" / "rerun",
    ROOT / "libexec" / "project-delete",
    ROOT / "libexec" / "project-rename",
    ROOT / "libexec" / "project-webhook-set",
    ROOT / "libexec" / "kilnr_project_lock.py",
    ROOT / "libexec" / "kilnr_permissions.py",
    ROOT / "libexec" / "git-key-add",
    ROOT / "libexec" / "secret-set",
    ROOT / "libexec" / "secret-set-file",
    ROOT / "libexec" / "secret-list",
    ROOT / "libexec" / "secret-delete",
    ROOT / "web" / "server" / "kilnr_web.py",
]


def load_script(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def check_project_rename_cli():
    cli = load_script(ROOT / "bin" / "kilnr", "kilnr_static_project_rename")
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert cli.usage() == 2
    assert "kilnr project rename <old-name> <new-name>" in stderr.getvalue()

    calls = []
    cli.privileged_command = lambda helper, *args, **kwargs: calls.append(
        (helper, args, kwargs)
    ) or 17
    original_argv = sys.argv
    sys.argv = ["kilnr", "project", "rename", "old_name", "new-name"]
    try:
        assert cli.main() == 17
    finally:
        sys.argv = original_argv
    assert calls == [
        (cli.PROJECT_RENAME, ("old_name", "new-name"), {}),
    ]

    for argv in (
        ["kilnr", "project", "rename", "old_name"],
        ["kilnr", "project", "rename", "old_name", "new-name", "extra"],
    ):
        sys.argv = argv
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                assert cli.main() == 2
        finally:
            sys.argv = original_argv
    assert len(calls) == 1


def check_project_rename_installation():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert (
        "for module in pipeline.py artifacts.py kilnr_secrets.py "
        "kilnr_project_lock.py kilnr_permissions.py; do"
    ) in install
    assert (
        'install -o root -g root -m 0644 "$ROOT_DIR/libexec/$module" '
        '"/usr/local/libexec/kilnr/$module"'
    ) in install

    executable_block = install[install.index("for name in \\\n") :]
    executable_block = executable_block[: executable_block.index("\ndone")]
    assert "project-rename" in executable_block
    assert (
        'install -o root -g root -m 0755 "$ROOT_DIR/libexec/$name" '
        '"/usr/local/libexec/kilnr/$name"'
    ) in executable_block

BASH_FILES = [
    ROOT / "install.sh",
    ROOT / "update.sh",
    ROOT / "uninstall.sh",
    ROOT / "install-web.sh",
    ROOT / "uninstall-web.sh",
    ROOT / "libexec" / "project-create",
    ROOT / "libexec" / "check-platform",
    ROOT / "libexec" / "doctor",
    ROOT / "libexec" / "network-setup",
    ROOT / "libexec" / "network-teardown",
    ROOT / "libexec" / "git-hooks" / "post-receive",
]


def main():
    failed = False

    for path in PYTHON_FILES:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            print(f"OK python: {path.relative_to(ROOT)}")
        except Exception as exc:
            failed = True
            print(f"FAIL python: {path.relative_to(ROOT)}: {exc}", file=sys.stderr)

    for path in BASH_FILES:
        result = subprocess.run(["bash", "-n", str(path)], check=False)
        if result.returncode == 0:
            print(f"OK bash:   {path.relative_to(ROOT)}")
        else:
            failed = True
            print(f"FAIL bash: {path.relative_to(ROOT)}", file=sys.stderr)

    json_files = [ROOT / "config" / "defaults.json"] + sorted((ROOT / "examples").glob("*.json"))
    for path in json_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            print(f"OK json:   {path.relative_to(ROOT)}")
            if path.parent.name == "examples" and isinstance(data, dict) and "steps" in data:
                failed = True
                print(f"FAIL schema: legacy top-level steps in {path.relative_to(ROOT)}", file=sys.stderr)
        except Exception as exc:
            failed = True
            print(f"FAIL json: {path.relative_to(ROOT)}: {exc}", file=sys.stderr)

    if (ROOT / "examples" / "pipeline.json").exists():
        failed = True
        print("FAIL schema: legacy examples/pipeline.json still exists", file=sys.stderr)

    execute_text = (ROOT / "libexec" / "execute").read_text(encoding="utf-8")
    forbidden = [
        "/var/run/docker.sock",
        "--privileged",
        "--network=host",
    ]
    for needle in forbidden:
        if needle in execute_text:
            failed = True
            print(f"FAIL security: execute contains forbidden token {needle!r}", file=sys.stderr)
    if "src=/etc/kilnr/secrets" in execute_text:
        failed = True
        print("FAIL security: execute mounts the persistent secrets directory", file=sys.stderr)
    if "dst=/artifacts" in execute_text:
        failed = True
        print("FAIL schema: execute still exposes legacy /artifacts mount", file=sys.stderr)
    if not failed:
        print("OK security: Docker runner contains no forbidden mounts/options")

    for description, check in (
        ("project rename CLI dispatch", check_project_rename_cli),
        ("project rename installation", check_project_rename_installation),
    ):
        try:
            check()
            print(f"OK static: {description}")
        except Exception as exc:
            failed = True
            print(f"FAIL static: {description}: {exc}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
