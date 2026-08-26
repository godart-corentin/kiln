#!/usr/bin/env python3
import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PYTHON_FILES = [
    ROOT / "bin" / "kiln",
    ROOT / "libexec" / "controller",
    ROOT / "libexec" / "pipeline.py",
    ROOT / "libexec" / "artifacts.py",
    ROOT / "libexec" / "secrets.py",
    ROOT / "libexec" / "enqueue",
    ROOT / "libexec" / "execute",
    ROOT / "libexec" / "notify-discord",
    ROOT / "libexec" / "rerun",
    ROOT / "libexec" / "project-delete",
    ROOT / "libexec" / "project-webhook-set",
    ROOT / "libexec" / "git-key-add",
    ROOT / "libexec" / "secret-set",
    ROOT / "libexec" / "secret-set-file",
    ROOT / "libexec" / "secret-list",
    ROOT / "libexec" / "secret-delete",
    ROOT / "web" / "web",
]

BASH_FILES = [
    ROOT / "install.sh",
    ROOT / "update.sh",
    ROOT / "uninstall.sh",
    ROOT / "install-web.sh",
    ROOT / "uninstall-web.sh",
    ROOT / "libexec" / "project-create",
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
    if "src=/etc/kiln/secrets" in execute_text:
        failed = True
        print("FAIL security: execute mounts the persistent secrets directory", file=sys.stderr)
    if "dst=/artifacts" in execute_text:
        failed = True
        print("FAIL schema: execute still exposes legacy /artifacts mount", file=sys.stderr)
    if not failed:
        print("OK security: Docker runner contains no forbidden mounts/options")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
