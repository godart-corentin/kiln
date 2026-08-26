#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -eq 0 ]]; then
    "$ROOT_DIR/install.sh" --update
else
    sudo "$ROOT_DIR/install.sh" --update
fi

if docker ps --format '{{.Names}}' 2>/dev/null | grep -Fxq 'kiln-web'; then
    if [[ -f /opt/kiln/docker-compose.yml ]] \
        && grep -q '^[[:space:]]*build:' /opt/kiln/docker-compose.yml
    then
        docker compose \
            -f /opt/kiln/docker-compose.yml \
            up -d --build kiln-web
        echo "Rebuilt kiln-web to load the updated React frontend and API."
    else
        echo "Kiln web update staged. Run install-web.sh once to migrate the existing web deployment to React."
    fi
fi
