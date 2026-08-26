#!/usr/bin/env bash
set -Eeuo pipefail

die() {
    echo "kiln web install: $*" >&2
    exit 1
}

[[ "${EUID}" -eq 0 ]] || die "run with sudo"
[[ "$#" -eq 1 ]] || die "usage: sudo ./install-web.sh <domain>"

DOMAIN="$1"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid domain"

CADDY_CONTAINER="${KILN_CADDY_CONTAINER:-caddy}"
NETWORK="${KILN_PROXY_NETWORK:-kiln-proxy}"
KILN_ROOT="/opt/kiln"
KILN_COMPOSE="${KILN_ROOT}/docker-compose.yml"
WEB_SCRIPT="/usr/local/libexec/kiln/web"
WEB_CONFIG="/etc/kiln/web.json"
AUTH_USER="${KILN_WEB_USER:-kiln}"

command -v docker >/dev/null || die "docker not found"
docker compose version >/dev/null 2>&1 || die "docker compose plugin unavailable"
[[ -x "$WEB_SCRIPT" ]] || die "Kiln web program is not installed; run install.sh first"
docker inspect "$CADDY_CONTAINER" >/dev/null 2>&1 || die "running Caddy container '$CADDY_CONTAINER' not found"

CADDY_WORKDIR="$(
    docker inspect "$CADDY_CONTAINER" \
        --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
)"
CADDY_COMPOSE_FILES="$(
    docker inspect "$CADDY_CONTAINER" \
        --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}'
)"
CADDY_SERVICE="$(
    docker inspect "$CADDY_CONTAINER" \
        --format '{{ index .Config.Labels "com.docker.compose.service" }}'
)"
[[ -n "$CADDY_SERVICE" ]] || CADDY_SERVICE="$CADDY_CONTAINER"
[[ -n "$CADDY_WORKDIR" ]] || die "cannot determine Caddy Compose working directory"
[[ -n "$CADDY_COMPOSE_FILES" ]] || die "cannot determine Caddy Compose file"

# The first config file is the base file; the override is placed beside it so
# future `docker compose up -d` commands keep the Kiln proxy network.
CADDY_COMPOSE="${CADDY_COMPOSE_FILES%%,*}"
CADDY_OVERRIDE="${CADDY_WORKDIR}/docker-compose.override.yml"

CADDYFILE="$(
    docker inspect "$CADDY_CONTAINER" \
        --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}'
)"
[[ -f "$CADDYFILE" ]] || die "cannot locate host Caddyfile mount"

if [[ -f "$CADDY_OVERRIDE" ]] && ! grep -q '# KILN MANAGED OVERRIDE' "$CADDY_OVERRIDE"; then
    die "$CADDY_OVERRIDE already exists and is not managed by Kiln; integrate kiln-proxy manually"
fi

KILN_WEB_UID="$(id -u kiln-web)"
KILN_WEB_GID="$(id -g kiln-web)"
KILN_READERS_GID="$(getent group kiln-readers | cut -d: -f3)"

mkdir -p "$KILN_ROOT"

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
    docker network create --driver bridge --internal "$NETWORK" >/dev/null
fi
[[ "$(docker network inspect "$NETWORK" --format '{{.Internal}}')" == "true" ]] \
    || die "existing network $NETWORK is not internal"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

TMP_OVERRIDE="$TMP_DIR/docker-compose.override.yml"
TMP_COMPOSE="$TMP_DIR/kiln-compose.yml"
TMP_CADDYFILE="$TMP_DIR/Caddyfile"

cat >"$TMP_OVERRIDE" <<EOF
# KILN MANAGED OVERRIDE
services:
  ${CADDY_SERVICE}:
    networks:
      kiln_proxy:

networks:
  kiln_proxy:
    external: true
    name: ${NETWORK}
EOF

cat >"$TMP_COMPOSE" <<EOF
services:
  kiln-web:
    image: python:3.12-alpine
    container_name: kiln-web
    restart: unless-stopped
    init: true
    user: "${KILN_WEB_UID}:${KILN_WEB_GID}"
    group_add:
      - "${KILN_READERS_GID}"
    command:
      - python3
      - /opt/kiln/web
    environment:
      KILN_WEB_HOST: "0.0.0.0"
      KILN_WEB_PORT: "8088"
    volumes:
      - type: bind
        source: /usr/local/libexec/kiln/web
        target: /opt/kiln/web
        read_only: true
      - type: bind
        source: /var/lib/kiln/builds
        target: /var/lib/kiln/builds
        read_only: true
    read_only: true
    tmpfs:
      - /tmp:size=16m,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 128
    mem_limit: 128m
    cpus: 0.50
    healthcheck:
      test:
        - CMD
        - python3
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/healthz', timeout=2).read()"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 5s
    networks:
      - kiln_proxy

networks:
  kiln_proxy:
    external: true
    name: ${NETWORK}
EOF

echo "Choose the password for https://${DOMAIN}"
echo "Username: ${AUTH_USER}"
read -rsp "Password: " PASSWORD
echo
read -rsp "Confirm password: " PASSWORD2
echo
[[ -n "$PASSWORD" ]] || die "password cannot be empty"
[[ "$PASSWORD" == "$PASSWORD2" ]] || die "passwords do not match"

CADDY_HASH="$(
    docker exec \
        -e KILN_PASSWORD="$PASSWORD" \
        "$CADDY_CONTAINER" \
        sh -c 'caddy hash-password --plaintext "$KILN_PASSWORD"'
)"
unset PASSWORD PASSWORD2
[[ -n "$CADDY_HASH" ]] || die "Caddy generated an empty password hash"

cp "$CADDYFILE" "$TMP_CADDYFILE"

write_caddy_block() {
    local directive="$1"
    DOMAIN="$DOMAIN" AUTH_USER="$AUTH_USER" CADDY_HASH="$CADDY_HASH" \
    AUTH_DIRECTIVE="$directive" CADDYFILE="$TMP_CADDYFILE" \
    python3 <<'PY'
import os, re
from pathlib import Path

path = Path(os.environ["CADDYFILE"])
text = path.read_text(encoding="utf-8")
text = re.sub(r"\n?# BEGIN KILN\n.*?# END KILN\n?", "\n", text, flags=re.DOTALL)

block = f"""# BEGIN KILN
{os.environ['DOMAIN']} {{
    {os.environ['AUTH_DIRECTIVE']} {{
        {os.environ['AUTH_USER']} {os.environ['CADDY_HASH']}
    }}

    encode zstd gzip
    reverse_proxy kiln-web:8088
}}
# END KILN
"""
path.write_text(text.rstrip() + "\n\n" + block, encoding="utf-8")
PY
}

CADDY_IMAGE="$(docker inspect "$CADDY_CONTAINER" --format '{{.Config.Image}}')"

validate_caddy() {
    docker run --rm \
        -v "${TMP_CADDYFILE}:/etc/caddy/Caddyfile:ro" \
        "$CADDY_IMAGE" \
        caddy validate --config /etc/caddy/Caddyfile \
        >/dev/null 2>&1
}

write_caddy_block "basic_auth"
if ! validate_caddy; then
    cp "$CADDYFILE" "$TMP_CADDYFILE"
    write_caddy_block "basicauth"
    validate_caddy || {
        docker run --rm \
            -v "${TMP_CADDYFILE}:/etc/caddy/Caddyfile:ro" \
            "$CADDY_IMAGE" \
            caddy validate --config /etc/caddy/Caddyfile
        die "generated Caddyfile is invalid"
    }
fi

docker compose -f "$CADDY_COMPOSE" -f "$TMP_OVERRIDE" config >/dev/null
docker compose -f "$TMP_COMPOSE" config >/dev/null

BACKUP_DIR="${KILN_ROOT}/backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp "$CADDYFILE" "$BACKUP_DIR/Caddyfile"
[[ -f "$CADDY_OVERRIDE" ]] && cp "$CADDY_OVERRIDE" "$BACKUP_DIR/docker-compose.override.yml"
[[ -f "$KILN_COMPOSE" ]] && cp "$KILN_COMPOSE" "$BACKUP_DIR/docker-compose.yml"

install -o root -g root -m 0644 "$TMP_OVERRIDE" "$CADDY_OVERRIDE"
install -o root -g root -m 0644 "$TMP_COMPOSE" "$KILN_COMPOSE"
install -o root -g root -m 0644 "$TMP_CADDYFILE" "$CADDYFILE"

cat >"$WEB_CONFIG" <<EOF
{
  "public_url": "https://${DOMAIN}"
}
EOF
chown root:root "$WEB_CONFIG"
chmod 0644 "$WEB_CONFIG"

docker compose -f "$KILN_COMPOSE" up -d

(
    cd "$CADDY_WORKDIR"
    docker compose up -d "$CADDY_SERVICE"
)

for _ in $(seq 1 30); do
    HEALTH="$(
        docker inspect kiln-web \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            2>/dev/null || true
    )"
    [[ "$HEALTH" == "healthy" ]] && break
    sleep 1
done

HEALTH="$(docker inspect kiln-web --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
[[ "$HEALTH" == "healthy" ]] || die "kiln-web did not become healthy (status: $HEALTH)"

[[ "$(docker inspect "$CADDY_CONTAINER" --format "{{if index .NetworkSettings.Networks \"${NETWORK}\"}}yes{{else}}no{{end}}")" == "yes" ]] \
    || die "Caddy is not connected to $NETWORK"
[[ "$(docker inspect kiln-web --format "{{if index .NetworkSettings.Networks \"${NETWORK}\"}}yes{{else}}no{{end}}")" == "yes" ]] \
    || die "kiln-web is not connected to $NETWORK"

echo
echo "Kiln Web installed."
echo "URL:      https://${DOMAIN}"
echo "Username: ${AUTH_USER}"
echo "Compose:  ${KILN_COMPOSE}"
echo "Caddy:    ${CADDYFILE}"
echo "Backup:   ${BACKUP_DIR}"
