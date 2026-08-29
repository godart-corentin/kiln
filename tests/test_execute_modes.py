#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import os
import stat
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXECUTE_PATH = ROOT / "libexec" / "execute"


def load_script():
    loader = importlib.machinery.SourceFileLoader("kilnr_execute_test", str(EXECUTE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


execute = load_script()


def test_write_json_clamps_metadata_mode():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "status.json"
        previous_umask = os.umask(0)
        try:
            execute.write_json(path, {"schema": 1})
        finally:
            os.umask(previous_umask)
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_run_script_keeps_commands_in_one_shell():
    script = execute.render_run_script([
        "cd frontend",
        "export NODE_ENV=test",
        "pnpm test",
    ])
    assert script.startswith("#!/bin/sh\nset -eu\n")
    assert script.index("cd frontend") < script.index("export NODE_ENV=test") < script.index("pnpm test")
    assert "$ cd frontend" in script
    assert "$ export NODE_ENV=test" in script
    assert "$ pnpm test" in script


def test_execution_argv_for_run_uses_generated_read_only_script():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        mounts, argv = execute.prepare_execution(
            build,
            "tests",
            {"run": ["echo ok"]},
        )
        assert argv == ["/bin/sh", "/run/kilnr/job.sh"]
        assert len(mounts) == 1
        mount = mounts[0]
        assert "dst=/run/kilnr/job.sh" in mount
        assert "readonly" in mount
        generated = build / "commands" / "tests.sh"
        assert generated.is_file()
        assert "echo ok" in generated.read_text()


def test_execution_argv_for_script_stays_inside_workspace():
    mounts, argv = execute.prepare_execution(
        Path("/tmp/build"),
        "package",
        {"script": "scripts/ci/package.sh"},
    )
    assert mounts == []
    assert argv == ["/workspace/scripts/ci/package.sh"]


def test_execution_argv_for_command_is_direct_argv():
    mounts, argv = execute.prepare_execution(
        Path("/tmp/build"),
        "tool",
        {"command": ["node", "tool.mjs", "--foo"]},
    )
    assert mounts == []
    assert argv == ["node", "tool.mjs", "--foo"]



def test_status_updates_target_pipeline_job():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        (build / "status.json").write_text(
            '{"pipeline":{"jobs":{"tests":{"state":"pending"}}}}\n',
            encoding="utf-8",
        )
        execute.update_job(build, "tests", {"state": "running"})
        import json
        status = json.loads((build / "status.json").read_text(encoding="utf-8"))
        assert status["pipeline"]["jobs"]["tests"]["state"] == "running"


def test_public_environment_includes_context_declared_env_and_input_paths():
    runtime = {
        "build_id": "build-1",
        "project": "demo",
        "sha": "a" * 40,
        "ref": "refs/heads/main",
        "job_type": "ci",
        "branch": "main",
    }
    job = {"env": {"NODE_ENV": "test"}, "secrets": ["APPLE_ID"]}
    input_roots = {"package-linux": Path("/tmp/linux")}
    env = execute.build_public_env(runtime, "tests", job, input_roots)
    assert env["CI"] == "true"
    assert env["HOME"] == "/run/kilnr/home"
    assert env["XDG_RUNTIME_DIR"] == "/run/kilnr/tmp"
    assert env["TMPDIR"] == "/run/kilnr/tmp"
    assert env["TMP"] == "/run/kilnr/tmp"
    assert env["TEMP"] == "/run/kilnr/tmp"
    assert env["KILNR_BUILD_ID"] == "build-1"
    assert env["KILNR_PROJECT"] == "demo"
    assert env["KILNR_SHA"] == "a" * 40
    assert env["KILNR_REF"] == "refs/heads/main"
    assert env["KILNR_JOB_TYPE"] == "ci"
    assert env["KILNR_JOB"] == "tests"
    assert env["KILNR_BRANCH"] == "main"
    assert "KILNR_TAG" not in env
    assert env["NODE_ENV"] == "test"
    assert "APPLE_ID" not in env
    assert env["KILNR_INPUT_PACKAGE_LINUX"] == "/run/kilnr/inputs/package-linux"


def test_input_mounts_are_read_only_and_separate():
    roots = {
        "linux": Path("/build/artifacts/linux"),
        "windows": Path("/build/artifacts/windows"),
    }
    mounts = execute.build_input_mounts(roots)
    assert len(mounts) == 2
    assert "src=/build/artifacts/linux,dst=/run/kilnr/inputs/linux,readonly" in mounts[0]
    assert "src=/build/artifacts/windows,dst=/run/kilnr/inputs/windows,readonly" in mounts[1]


def test_collect_job_artifacts_uses_workspace_not_special_mount():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        work = build / "work" / "package"
        (work / "release").mkdir(parents=True)
        (work / "release" / "demo.AppImage").write_text("binary", encoding="utf-8")
        collected = execute.collect_job_artifacts(
            build,
            work,
            "package",
            {"artifacts": ["release/*.AppImage"]},
        )
        assert collected == ["release/demo.AppImage"]
        assert (build / "artifacts" / "package" / "release" / "demo.AppImage").is_file()


def test_secret_wrapper_contains_names_and_paths_but_no_values():
    wrapper = execute.render_secret_wrapper({
        "APPLE_ID": {"kind": "text", "scope": "release"},
        "CSC_LINK": {"kind": "file", "scope": "release"},
    })
    assert 'export APPLE_ID="$(cat /run/kilnr/secrets/APPLE_ID.value)"' in wrapper
    assert 'export CSC_LINK="/run/kilnr/secrets/CSC_LINK.value"' in wrapper
    assert 'exec "$@"' in wrapper
    assert "actual-secret" not in wrapper


def test_prepare_secret_stage_is_outside_builds_and_contains_only_requested_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        secrets_root = root / "etc-secrets"
        staging_root = root / "staging"
        (secrets_root / "demo").mkdir(parents=True)

        old_root = execute.SECRETS_ROOT
        old_stage = execute.SECRET_STAGING
        try:
            execute.SECRETS_ROOT = secrets_root
            execute.SECRET_STAGING = staging_root
            execute.secret_schema.store_secret(
                secrets_root, "demo", "APPLE_ID", b"dev@example.com",
                kind="text", scope="release"
            )
            stage, metadata, redact = execute.prepare_secret_stage(
                "build-1", "release", {"project": "demo", "job_type": "release"},
                {"secrets": ["APPLE_ID"]},
            )
        finally:
            execute.SECRETS_ROOT = old_root
            execute.SECRET_STAGING = old_stage

        assert stage == staging_root / "build-1" / "release"
        assert (stage / "APPLE_ID.value").read_bytes() == b"dev@example.com"
        assert metadata["APPLE_ID"]["kind"] == "text"
        assert "dev@example.com" in redact
        assert not list(stage.glob("*.json"))


def test_redaction_masks_known_secret_tokens():
    tokens = execute.redaction_tokens(["dev@example.com", "line1\nline2"])
    text = "login dev@example.com\nline1\nline2\n"
    redacted = execute.redact_text(text, tokens)
    assert "dev@example.com" not in redacted
    assert "line1" not in redacted
    assert "line2" not in redacted
    assert "***" in redacted

def test_tools_wrapper_keeps_corepack_home_on_executable_tmpfs():
    wrapper = execute.render_tools_wrapper({"pnpm": "11.15.1"})

    assert 'TOOLS_ROOT="/run/kilnr/tmp/tools"' in wrapper
    assert 'COREPACK_HOME="$TOOLS_ROOT/corepack"' in wrapper
    assert 'PATH="/run/kilnr/tools/bin:$PATH"' in wrapper

    assert 'TOOLS_ROOT="/tmp/' not in wrapper
    assert "cat >" not in wrapper
    assert "chmod" not in wrapper

def test_prepare_tools_wrapper_mounts_executable_tools_read_only():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)

        mounts, argv = execute.prepare_tools_wrapper(
            build,
            "tests",
            {"pnpm": "11.15.1"},
            ["/bin/sh", "/run/kilnr/job.sh"],
        )

        assert len(mounts) == 2

        assert any(
            "dst=/run/kilnr/tools-wrapper.sh" in mount
            and "readonly" in mount
            for mount in mounts
        )

        assert any(
            "dst=/run/kilnr/tools" in mount
            and "readonly" in mount
            for mount in mounts
        )

        pnpm = build / "runtime" / "tests" / "tools" / "bin" / "pnpm"

        assert pnpm.is_file()
        assert pnpm.stat().st_mode & 0o100
        assert "corepack pnpm@11.15.1" in pnpm.read_text(
            encoding="utf-8"
        )

        assert argv == [
            "/bin/sh",
            "/run/kilnr/tools-wrapper.sh",
            "/bin/sh",
            "/run/kilnr/job.sh",
        ]

def test_runner_security_flags_remain_present():
    text = EXECUTE_PATH.read_text(encoding="utf-8")
    for token in (
        '"--rm"',
        '"--init"',
        '"--cpus"',
        '"--memory"',
        '"--pids-limit"',
        '"--cap-drop", "ALL"',
        '"no-new-privileges=true"',
    ):
        assert token in text, token
    assert '"--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=512m"' in text
    assert '"--tmpfs", f"/run/kilnr/tmp:rw,nosuid,nodev,exec,size=512m,uid={uid},gid={gid},mode=700"' in text
    assert (
        'f"/run/kilnr/home:rw,nosuid,nodev,exec,size=512m,uid={uid},gid={gid},mode=700"'
        not in text
    )
    assert "dst=/run/kilnr/home" in text
    assert "/var/run/docker.sock" not in text
    assert "--privileged" not in text


def main():
    tests = [
        test_write_json_clamps_metadata_mode,
        test_run_script_keeps_commands_in_one_shell,
        test_execution_argv_for_run_uses_generated_read_only_script,
        test_execution_argv_for_script_stays_inside_workspace,
        test_execution_argv_for_command_is_direct_argv,
        test_status_updates_target_pipeline_job,
        test_public_environment_includes_context_declared_env_and_input_paths,
        test_input_mounts_are_read_only_and_separate,
        test_collect_job_artifacts_uses_workspace_not_special_mount,
        test_secret_wrapper_contains_names_and_paths_but_no_values,
        test_prepare_secret_stage_is_outside_builds_and_contains_only_requested_files,
        test_redaction_masks_known_secret_tokens,
        test_tools_wrapper_keeps_corepack_home_on_executable_tmpfs,
        test_prepare_tools_wrapper_mounts_executable_tools_read_only,
        test_runner_security_flags_remain_present,
    ]
    for test in tests:
        test()
        print(f"OK execute modes: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
