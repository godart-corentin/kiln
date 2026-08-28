#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(
    cd "$(dirname "${BASH_SOURCE[0]}")/.." \
    && pwd
)"

for test in \
    test_static.py \
    test_uninstall.py \
    test_install_web.py \
    test_web_api.py \
    test_web_http.py \
    test_frontend.py \
    test_pipeline.py \
    test_cache.py \
    test_artifacts.py \
    test_secrets.py \
    test_import_shadowing.py \
    test_branch_pipelines.py \
    test_controller_dag.py \
    test_execute_modes.py \
    test_job_status_consumers.py \
    test_enqueue_atomic_publish.py
do
    python3 "$ROOT_DIR/tests/$test"
done

for unit in \
    "$ROOT_DIR/systemd/kilnr-controller.service" \
    "$ROOT_DIR/systemd/kilnr-queue.path" \
    "$ROOT_DIR/systemd/kilnr-network.service"
do
    grep -q \
        '^\[Unit\]$' \
        "$unit"
done

echo "OK systemd unit sections"
