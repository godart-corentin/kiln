#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "uninstall.sh").read_text(encoding="utf-8")


def main():
    required = [
        'Usage: sudo ./uninstall.sh [--purge [--yes]]',
        '[[ "$confirmation" != "PURGE" ]]',
        '[[ "$ASSUME_YES" -eq 1 && "$PURGE" -ne 1 ]]',
        "/usr/local/share/kilnr",
        "/opt/kilnr",
        "/var/lib/kilnr",
        "/etc/kilnr",
        "/srv/git",
        "for user in kilnr-web kilnr",
        "for group in kilnr-readers kilnr-submit kilnr-web kilnr",
    ]
    for fragment in required:
        assert fragment in SCRIPT, f"uninstall purge is missing: {fragment}"

    assert SCRIPT.index('[[ "$confirmation" != "PURGE" ]]') < SCRIPT.index(
        "rm -rf \\\n        /usr/local/share/kilnr"
    )
    print("OK uninstall: purge is explicit, guarded, and comprehensive")


if __name__ == "__main__":
    main()
