#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -eq 0 ]]; then
    "$ROOT_DIR/install.sh" --update
else
    sudo "$ROOT_DIR/install.sh" --update
fi

if docker ps --format '{{.Names}}' 2>/dev/null | grep -Fxq 'kiln-web'; then
    docker restart kiln-web >/dev/null
    echo "Restarted kiln-web to load the updated web program."
fi
