#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIBEXEC = ROOT / "libexec"


def test_libexec_does_not_shadow_stdlib_secrets():
    assert not (LIBEXEC / "secrets.py").exists(), (
        "libexec/secrets.py shadows Python's stdlib secrets module for "
        "scripts such as enqueue that use secrets.token_hex()"
    )
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "rm -f /usr/local/libexec/kiln/secrets.py" in install


if __name__ == "__main__":
    test_libexec_does_not_shadow_stdlib_secrets()
    print("OK: libexec does not shadow stdlib secrets")
