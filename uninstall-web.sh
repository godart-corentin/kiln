#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${EUID}" -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

KILN_ROOT="/opt/kiln"
KILN_COMPOSE="${KILN_ROOT}/docker-compose.yml"
NETWORK="${KILN_PROXY_NETWORK:-kiln-proxy}"
CADDY_CONTAINER="${KILN_CADDY_CONTAINER:-caddy}"

CADDY_SERVICE="$(
    docker inspect "$CADDY_CONTAINER"         --format '{{ index .Config.Labels "com.docker.compose.service" }}'         2>/dev/null || true
)"
[[ -n "$CADDY_SERVICE" ]] || CADDY_SERVICE="$CADDY_CONTAINER"

if [[ -f "$KILN_COMPOSE" ]]; then
    docker compose -f "$KILN_COMPOSE" down || true
fi

CADDY_WORKDIR="$(
    docker inspect "$CADDY_CONTAINER" \
        --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' \
        2>/dev/null || true
)"
CADDYFILE="$(
    docker inspect "$CADDY_CONTAINER" \
        --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' \
        2>/dev/null || true
)"

if [[ -n "$CADDYFILE" && -f "$CADDYFILE" ]]; then
    python3 - "$CADDYFILE" <<'PY'
import re, sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = re.sub(r"\n?# BEGIN KILN\n.*?# END KILN\n?", "\n", text, flags=re.DOTALL)
path.write_text(text.rstrip() + "\n", encoding="utf-8")
PY
fi

if [[ -n "$CADDY_WORKDIR" ]]; then
    override="${CADDY_WORKDIR}/docker-compose.override.yml"
    if [[ -f "$override" ]] && grep -q '# KILN MANAGED OVERRIDE' "$override"; then
        rm -f "$override"
    fi
    (cd "$CADDY_WORKDIR" && docker compose up -d "$CADDY_SERVICE") || true
fi

docker network rm "$NETWORK" >/dev/null 2>&1 || true
rm -f /etc/kiln/web.json

echo "Kiln Web removed. /opt/kiln backups were preserved."
