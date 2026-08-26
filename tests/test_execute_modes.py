#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXECUTE_PATH = ROOT / "libexec" / "execute"


def load_script():
    loader = importlib.machinery.SourceFileLoader("kiln_execute_test", str(EXECUTE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


execute = load_script()


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
        assert argv == ["/bin/sh", "/run/kiln/job.sh"]
        assert len(mounts) == 1
        mount = mounts[0]
        assert "dst=/run/kiln/job.sh" in mount
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
    assert "/var/run/docker.sock" not in text
    assert "--privileged" not in text


def main():
    tests = [
        test_run_script_keeps_commands_in_one_shell,
        test_execution_argv_for_run_uses_generated_read_only_script,
        test_execution_argv_for_script_stays_inside_workspace,
        test_execution_argv_for_command_is_direct_argv,
        test_status_updates_target_pipeline_job,
        test_runner_security_flags_remain_present,
    ]
    for test in tests:
        test()
        print(f"OK execute modes: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
