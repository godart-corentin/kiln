#!/usr/bin/env python3

import re
import sys
from pathlib import Path


ROOT = Path(
    __file__
).resolve().parents[1]

INSTALLER = (
    ROOT
    / "install-web.sh"
)


def fail(message):
    print(
        f"FAIL install-web: {message}",
        file=sys.stderr,
    )

    raise SystemExit(1)


text = INSTALLER.read_text(
    encoding="utf-8"
)


#
# Extract the generated Caddy Compose override heredoc.
#

match = re.search(
    r'''
    cat\s+>"\$TMP_OVERRIDE"\s+<<EOF
    \n
    (?P<body>.*?)
    \nEOF
    ''',
    text,
    flags=re.DOTALL | re.VERBOSE,
)

if match is None:
    fail(
        "cannot find TMP_OVERRIDE template"
    )


override = match.group(
    "body"
)


#
# Regression:
#
# Kiln must ADD its proxy network without replacing Caddy's normal
# Compose default network.
#

network_block = re.search(
    r'''
    services:
    \s*
    \$\{CADDY_SERVICE\}:
    \s*
    networks:
    \s*
    default:
    \s*
    kiln_proxy:
    ''',
    override,
    flags=re.VERBOSE,
)

if network_block is None:
    fail(
        "Caddy override must preserve both default and kiln_proxy networks"
    )


if "name: ${NETWORK}" not in override:
    fail(
        "kiln_proxy must reference the configured external network"
    )


if "# KILN MANAGED OVERRIDE" not in override:
    fail(
        "override ownership marker is missing"
    )


#
# The semantic Compose validation must use a file for JSON data.
#
# A previous implementation piped JSON into Python while also using
# a heredoc for the Python program:
#
#     docker compose ... | python3 <<'PY'
#
# stdin was therefore consumed by the program source instead of the
# Compose JSON, causing JSONDecodeError.
#

if 'MERGED_COMPOSE_JSON="${TMP_DIR}/merged-compose.json"' not in text:
    fail(
        "merged Compose JSON must be written to a temporary file"
    )


if '>"$MERGED_COMPOSE_JSON"' not in text:
    fail(
        "docker compose JSON output is not written to MERGED_COMPOSE_JSON"
    )


if 'python3 - "$MERGED_COMPOSE_JSON"' not in text:
    fail(
        "Compose network validator must read MERGED_COMPOSE_JSON as a file"
    )


#
# Backup path must use a normal single-line Bash parameter expansion.
#

expected_backup = (
    'BACKUP_DIR="${KILN_ROOT}/backups/'
    '$(date +%Y%m%d-%H%M%S)"'
)

if expected_backup not in text:
    fail(
        "BACKUP_DIR uses an unsafe or malformed parameter expansion"
    )


#
# Docker inspect currently emits a trailing blank line for the network
# range template. The installer must ignore empty network names.
#

if "sed '/^[[:space:]]*$/d'" not in text:
    fail(
        "Caddy network discovery does not filter blank lines"
    )


if '[[ -n "$original_network" ]] || continue' not in text:
    fail(
        "Caddy network verification does not defensively skip empty names"
    )


#
# Runtime guards:
#
# The installer should remember Caddy's original networks and verify
# them again after Compose recreates the container.
#

required_fragments = [
    "CADDY_ORIGINAL_NETWORKS",
    "Caddy lost its pre-existing Docker network",
    'grep -Fxq "default"',
    'grep -Fxq "kiln_proxy"',
]

for fragment in required_fragments:
    if fragment not in text:
        fail(
            f"missing runtime network guard: {fragment}"
        )


print(
    "OK install-web: Caddy networks are preserved"
)
# React UI is built into a dedicated image. Runtime must not depend on Node
# or bind the Python source from the host.
compose_match = re.search(
    r'''cat\s+>"\$TMP_COMPOSE"\s+<<EOF\n(?P<body>.*?)\nEOF''',
    text,
    flags=re.DOTALL | re.VERBOSE,
)
if compose_match is None:
    fail("cannot find TMP_COMPOSE template")

kiln_compose = compose_match.group("body")
required_web_fragments = [
    "build:",
    "context: ${WEB_SOURCE}",
    "dockerfile: Dockerfile",
    "image: kiln-web:local",
    'KILN_WEB_STATIC: "/opt/kiln/static"',
    "source: /var/lib/kiln/builds",
    "target: /var/lib/kiln/builds",
    "read_only: true",
]
for fragment in required_web_fragments:
    if fragment not in kiln_compose:
        fail(f"missing React web runtime fragment: {fragment}")

for forbidden in (
    "/usr/local/libexec/kiln/web",
    "/var/run/docker.sock",
    "/etc/kiln/secrets",
    "/srv/git",
    "/var/lib/kiln/queue",
):
    if forbidden in kiln_compose:
        fail(f"kiln-web compose exposes forbidden path: {forbidden}")

if "up -d --build" not in text:
    fail("install-web must build the React image before starting kiln-web")

print("OK install-web: React image keeps kiln-web read-only and isolated")
