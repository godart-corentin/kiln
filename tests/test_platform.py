#!/usr/bin/env python3
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_PLATFORM = ROOT / "libexec" / "check-platform"


def run_platform(os_release):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "os-release"
        path.write_text(os_release, encoding="utf-8")
        return subprocess.run(
            ["bash", str(CHECK_PLATFORM), str(path)],
            check=False,
            capture_output=True,
            text=True,
        )


for version in ("24.04", "26.04"):
    result = run_platform(f'ID=ubuntu\nVERSION_ID="{version}"\n')
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", result.stderr

result = run_platform('ID=ubuntu\nVERSION_ID="25.10"\n')
assert result.returncode == 0, result.stderr
assert "supported on Ubuntu 24.04 and 26.04 LTS" in result.stderr
assert "found 25.10" in result.stderr

result = run_platform('ID=debian\nVERSION_ID="13"\n')
assert result.returncode != 0
assert "this installer targets Ubuntu" in result.stderr

install = (ROOT / "install.sh").read_text(encoding="utf-8")
assert '"$ROOT_DIR/libexec/check-platform"' in install

print("OK platform compatibility")
