#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import json
import os
import stat
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS_MODULE = ROOT / "libexec" / "kilnr_secrets.py"
CLI_PATH = ROOT / "bin" / "kilnr"


def load_module(path, name):
    if path.suffix == ".py":
        spec = importlib.util.spec_from_file_location(name, path)
    else:
        loader = importlib.machinery.SourceFileLoader(name, str(path))
        spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


secrets = load_module(SECRETS_MODULE, "kilnr_secrets_test")
cli = load_module(CLI_PATH, "kilnr_cli_secrets_test")


def test_store_list_load_and_delete_secret():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project_dir = root / "demo"
        project_dir.mkdir(mode=0o750)

        secrets.store_secret(
            root,
            "demo",
            "APPLE_ID",
            b"dev@example.com",
            kind="text",
            scope="release",
        )

        listed = secrets.list_secrets(root, "demo")
        assert listed == [{"name": "APPLE_ID", "scope": "release", "kind": "text"}]
        metadata = secrets.load_secret_metadata(root, "demo", "APPLE_ID")
        assert metadata == {"schema": 1, "scope": "release", "kind": "text"}
        assert "dev@example.com" not in json.dumps(metadata)
        assert secrets.read_secret_bytes(root, "demo", "APPLE_ID") == b"dev@example.com"
        mode = stat.S_IMODE((project_dir / "APPLE_ID.value").stat().st_mode)
        assert mode == 0o640

        secrets.delete_secret(root, "demo", "APPLE_ID")
        assert secrets.list_secrets(root, "demo") == []


def test_text_secret_rejects_nul_and_names_are_strict():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "demo").mkdir()
        for bad_name in ("bad-name", "lower", "KILNR_SHA"):
            try:
                secrets.store_secret(root, "demo", bad_name, b"x", kind="text", scope="release")
            except secrets.SecretError:
                pass
            else:
                raise AssertionError(f"expected invalid secret name {bad_name!r}")
        try:
            secrets.store_secret(root, "demo", "TOKEN", b"a\x00b", kind="text", scope="release")
        except secrets.SecretError as exc:
            assert "NUL" in str(exc)
        else:
            raise AssertionError("expected NUL text secret to fail")


def test_release_scope_policy_is_enforced():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "demo").mkdir()
        secrets.store_secret(root, "demo", "TOKEN", b"secret", kind="text", scope="release")
        assert secrets.validate_requested_secrets(root, "demo", ["TOKEN"], "release")["TOKEN"]["scope"] == "release"
        try:
            secrets.validate_requested_secrets(root, "demo", ["TOKEN"], "ci")
        except secrets.SecretError as exc:
            assert "release-only" in str(exc)
        else:
            raise AssertionError("expected CI access to release secret to fail")


def test_cli_secret_set_uses_hidden_input_and_stdin_not_argv():
    calls = []
    old_getpass = cli.getpass.getpass
    old_privileged = cli.privileged_command
    try:
        cli.getpass.getpass = lambda prompt: "super-secret"
        def fake_privileged(helper, *args, stdin_text=None):
            calls.append((helper, args, stdin_text))
            return 0
        cli.privileged_command = fake_privileged
        assert cli.secret_set("demo", "TOKEN") == 0
    finally:
        cli.getpass.getpass = old_getpass
        cli.privileged_command = old_privileged

    helper, args, stdin_text = calls[0]
    assert helper.endswith("/secret-set")
    assert args == ("demo", "TOKEN")
    assert "super-secret" not in " ".join(args)
    assert stdin_text == "super-secret"


def test_cli_usage_lists_secret_commands():
    import contextlib
    import io
    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        cli.usage()
    text = stream.getvalue()
    assert "kilnr secret set <project> <name>" in text
    assert "kilnr secret set-file <project> <name> <path>" in text
    assert "kilnr secret list <project>" in text
    assert "kilnr secret delete <project> <name>" in text



def test_install_and_project_lifecycle_wire_secret_storage():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    create = (ROOT / "libexec" / "project-create").read_text(encoding="utf-8")
    delete = (ROOT / "libexec" / "project-delete").read_text(encoding="utf-8")
    uninstall = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert "/var/lib/kilnr/secret-staging" in install
    assert 'for project_config in /etc/kilnr/projects/*.json' in install
    for name in ("secret-set", "secret-set-file", "secret-list", "secret-delete"):
        assert name in install
    assert 'secret_dir="${SECRETS_ROOT}/${project}"' in create
    assert 'install -d -o root -g kilnr -m 0750 "$secret_dir"' in create
    assert 'secret_dir = SECRETS_ROOT / project' in delete
    assert 'shutil.rmtree(secret_dir)' in delete
    assert 'rm -rf /var/lib/kilnr/secret-staging' in uninstall

def main():
    tests = [
        test_store_list_load_and_delete_secret,
        test_text_secret_rejects_nul_and_names_are_strict,
        test_release_scope_policy_is_enforced,
        test_cli_secret_set_uses_hidden_input_and_stdin_not_argv,
        test_cli_usage_lists_secret_commands,
        test_install_and_project_lifecycle_wire_secret_storage,
    ]
    for test in tests:
        test()
        print(f"OK secrets: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
