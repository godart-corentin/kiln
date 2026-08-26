#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-install}"

die() {
    echo "kiln install: $*" >&2
    exit 1
}

[[ "${EUID}" -eq 0 ]] || die "run with sudo"
[[ "$MODE" == "install" || "$MODE" == "--update" ]] || die "usage: sudo ./install.sh [--update]"

if [[ "$MODE" == "--update" ]] && systemctl is-active --quiet kiln-controller.service 2>/dev/null; then
    die "a Kiln build is currently running; wait for kiln-controller.service to become inactive"
fi

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    [[ "${ID:-}" == "ubuntu" ]] || die "this installer targets Ubuntu"
    if [[ "${VERSION_ID:-}" != "24.04" ]]; then
        echo "kiln install: warning: tested on Ubuntu 24.04 LTS, found ${VERSION_ID:-unknown}" >&2
    fi
fi

command -v apt-get >/dev/null || die "apt-get not found"

if [[ "$MODE" != "--update" ]]; then
    apt-get update
fi
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    git make python3 acl curl iptables

command -v docker >/dev/null || die "Docker is not installed. Install Docker first; Kiln will not alter the daemon configuration."
docker info >/dev/null 2>&1 || die "Docker daemon is not reachable"
getent group docker >/dev/null || die "Docker group is missing; this installer expects a rootful Docker Engine"
command -v systemctl >/dev/null || die "systemd is required"
command -v git-shell >/dev/null || die "git-shell is missing"

ensure_group() {
    local name="$1"
    getent group "$name" >/dev/null || groupadd --system "$name"
}

ensure_group git
ensure_group kiln
ensure_group kiln-web
ensure_group kiln-submit
ensure_group kiln-readers

ensure_user() {
    local name="$1"
    local home="$2"
    local shell="$3"
    local group="$4"

    if getent passwd "$name" >/dev/null; then
        local current_home current_shell
        current_home="$(getent passwd "$name" | cut -d: -f6)"
        current_shell="$(getent passwd "$name" | cut -d: -f7)"
        [[ "$current_home" == "$home" ]] || die "existing user '$name' has home $current_home, expected $home"
        [[ "$current_shell" == "$shell" ]] || die "existing user '$name' has shell $current_shell, expected $shell"
    else
        useradd \
            --system \
            --gid "$group" \
            --home-dir "$home" \
            --no-create-home \
            --shell "$shell" \
            "$name"
    fi
}

ensure_user git /srv/git /usr/bin/git-shell git
ensure_user kiln /var/lib/kiln /usr/sbin/nologin kiln
ensure_user kiln-web /var/lib/kiln-web /usr/sbin/nologin kiln-web

usermod -aG kiln-submit git
usermod -aG kiln-submit kiln
usermod -aG kiln-readers kiln-web

if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]] && getent passwd "${SUDO_USER}" >/dev/null; then
    usermod -aG kiln-readers "${SUDO_USER}"
fi

install -d -o root -g root -m 0755 /srv/git
install -d -o git -g git -m 0700 /srv/git/.ssh
if [[ ! -e /srv/git/.ssh/authorized_keys ]]; then
    install -o git -g git -m 0600 /dev/null /srv/git/.ssh/authorized_keys
fi
chown git:git /srv/git/.ssh/authorized_keys
chmod 0600 /srv/git/.ssh/authorized_keys

install -d -o kiln -g kiln-submit -m 0710 /var/lib/kiln
install -d -o kiln -g kiln-submit -m 0710 /var/lib/kiln/queue
install -d -o kiln -g kiln-submit -m 3730 \
    /var/lib/kiln/queue/tmp \
    /var/lib/kiln/queue/incoming
install -d -o kiln -g kiln -m 0750 \
    /var/lib/kiln/queue/running \
    /var/lib/kiln/builds \
    /var/lib/kiln/locks
install -d -o kiln -g kiln -m 0700 /var/lib/kiln/secret-staging

install -d -o root -g root -m 0755 /etc/kiln /etc/kiln/projects
install -d -o root -g kiln -m 0750 /etc/kiln/secrets
for project_config in /etc/kiln/projects/*.json; do
    [[ -e "$project_config" ]] || continue
    project_name="$(basename "$project_config" .json)"
    [[ "$project_name" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || continue
    install -d -o root -g kiln -m 0750 "/etc/kiln/secrets/$project_name"
done
install -d -o root -g root -m 0755 /usr/local/libexec/kiln /usr/local/libexec/kiln/git-hooks
# Remove the old project module name: it shadows Python stdlib secrets in enqueue.
rm -f /usr/local/libexec/kiln/secrets.py

# CLI readers can traverse Kiln state and read build output, but not queue/secrets.
setfacl -m g:kiln-readers:--x /var/lib/kiln
setfacl -m g:kiln-readers:r-x,d:g:kiln-readers:r-x /var/lib/kiln/builds
setfacl -R -m g:kiln-readers:rX /var/lib/kiln/builds
setfacl -m u:git:rwx /var/lib/kiln/queue/incoming
find /var/lib/kiln/builds -type d -exec chmod g-s {} +

install -o root -g root -m 0755 "$ROOT_DIR/bin/kiln" /usr/local/bin/kiln
install -o root -g root -m 0755 "$ROOT_DIR/web/server/kiln_web.py" /usr/local/libexec/kiln/web

install -d -o root -g root -m 0755 /usr/local/share/kiln
rm -rf /usr/local/share/kiln/web-src
cp -R "$ROOT_DIR/web" /usr/local/share/kiln/web-src
rm -rf \
    /usr/local/share/kiln/web-src/frontend/node_modules \
    /usr/local/share/kiln/web-src/frontend/dist \
    /usr/local/share/kiln/web-src/server/__pycache__
chown -R root:root /usr/local/share/kiln/web-src
find /usr/local/share/kiln/web-src -type d -exec chmod 0755 {} +
find /usr/local/share/kiln/web-src -type f -exec chmod 0644 {} +
chmod 0755 /usr/local/share/kiln/web-src/server/kiln_web.py

for module in pipeline.py artifacts.py kiln_secrets.py; do
    install -o root -g root -m 0644 "$ROOT_DIR/libexec/$module" "/usr/local/libexec/kiln/$module"
done

for name in \
    controller enqueue execute notify-discord rerun doctor \
    project-create project-delete project-webhook-set \
    secret-set secret-set-file secret-list secret-delete \
    git-key-add network-setup network-teardown
do
    install -o root -g root -m 0755 "$ROOT_DIR/libexec/$name" "/usr/local/libexec/kiln/$name"
done

install -o root -g root -m 0755 \
    "$ROOT_DIR/libexec/git-hooks/post-receive" \
    /usr/local/libexec/kiln/git-hooks/post-receive

if [[ ! -f /etc/kiln/defaults.json ]]; then
    install -o root -g root -m 0644 "$ROOT_DIR/config/defaults.json" /etc/kiln/defaults.json
fi

# Configure the isolated CI subnet once. Override on first install with:
#   sudo KILN_CI_SUBNET=172.31.50.0/24 ./install.sh
if [[ ! -f /etc/kiln/network.env ]]; then
    SUBNET="${KILN_CI_SUBNET:-172.30.0.0/24}"
    GATEWAY="$(
        /usr/bin/python3 - "$SUBNET" <<'PY'
import ipaddress, sys
net = ipaddress.ip_network(sys.argv[1], strict=True)
if net.version != 4 or net.prefixlen > 28:
    raise SystemExit("Kiln CI subnet must be an IPv4 network of /28 or larger")
print(next(net.hosts()))
PY
    )"

    cat >/etc/kiln/network.env <<EOF
NETWORK=kiln-ci
BRIDGE=kiln0
SUBNET=${SUBNET}
GATEWAY=${GATEWAY}
EOF
    chown root:root /etc/kiln/network.env
    chmod 0644 /etc/kiln/network.env
fi

for unit in kiln-controller.service kiln-queue.path kiln-network.service; do
    install -o root -g root -m 0644 "$ROOT_DIR/systemd/$unit" "/etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable --now kiln-network.service
systemctl enable --now kiln-queue.path

echo
echo "Kiln core installed."
echo
echo "Next:"
echo "  kiln git-key add"
echo "  kiln project create my_app"
echo
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    echo "Your user '${SUDO_USER}' was added to kiln-readers."
    echo "Reconnect your shell before using 'kiln status' if the group is not visible yet."
fi
echo "Optional web UI:"
echo "  sudo ./install-web.sh kiln.example.com"
