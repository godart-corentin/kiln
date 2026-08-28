#!/usr/bin/env python3
import contextlib
import importlib.machinery
import importlib.util
import io
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load_script("kilnr_cli_status_test", ROOT / "bin" / "kilnr")
notify = load_script("kilnr_notify_status_test", ROOT / "libexec" / "notify-discord")


def sample_status(state="running", job_state="running"):
    return {
        "schema": 1,
        "build_id": "build-1",
        "job_id": "build-1",
        "project": "demo",
        "sha": "a" * 40,
        "ref": "refs/heads/main",
        "type": "ci",
        "state": state,
        "started_at": "2026-08-26T00:00:00Z",
        "duration_seconds": None,
        "prepare": {"state": "success", "log": "logs/prepare.log"},
        "pipeline_path": ".kilnr/pipelines/ci.json",
        "pipeline": {
            "groups": {"quality": ["tests"]},
            "jobs": {
                "tests": {
                    "group": "quality",
                    "needs": [],
                    "resolved_needs": [],
                    "state": job_state,
                    "duration_seconds": None,
                    "log": "logs/tests.log",
                }
            },
        },
    }


def test_cli_status_lists_jobs_and_groups():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        cli.BUILDS = build.parent
        (build / "status.json").write_text(__import__("json").dumps(sample_status()), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.show_status(build)
    text = out.getvalue()
    assert "tests" in text
    assert "quality" in text
    assert "Steps" not in text


def test_cli_terminal_state_reads_pipeline_job():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        (build / "status.json").write_text(__import__("json").dumps(sample_status(job_state="success")), encoding="utf-8")
        assert cli.terminal_state(build, "tests") == "success"


def test_discord_groups_jobs_by_group():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        (build / "logs").mkdir()
        (build / "logs" / "tests.log").write_text("ok\n", encoding="utf-8")
        text = notify.render_message(build, sample_status(state="success", job_state="success"))
    assert "**Quality**" in text
    assert "tests" in text



def main():
    tests = [
        test_cli_status_lists_jobs_and_groups,
        test_cli_terminal_state_reads_pipeline_job,
        test_discord_groups_jobs_by_group,
    ]
    for test in tests:
        test()
        print(f"OK job status consumers: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
