#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(
    cd "$(dirname "${BASH_SOURCE[0]}")/.." \
    && pwd
)"

python3 \
    "$ROOT_DIR/tests/test_static.py"

python3 \
    "$ROOT_DIR/tests/test_install_web.py"

python3 \
    "$ROOT_DIR/tests/test_branch_pipelines.py"


for unit in \
    "$ROOT_DIR/systemd/kiln-controller.service" \
    "$ROOT_DIR/systemd/kiln-queue.path" \
    "$ROOT_DIR/systemd/kiln-network.service"
do
    grep -q \
        '^\[Unit\]$' \
        "$unit"
done

echo "OK systemd unit sections"
