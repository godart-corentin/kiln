#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(
    cd "$(dirname "${BASH_SOURCE[0]}")/.." \
    && pwd
)"

for test in \
    test_static.py \
    test_install_web.py \
    test_pipeline.py \
    test_branch_pipelines.py \
    test_controller_dag.py \
    test_execute_modes.py \
    test_job_status_consumers.py \
    test_enqueue_atomic_publish.py
do
    python3 "$ROOT_DIR/tests/$test"
done

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
