#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${EUID}" -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

systemctl disable --now kiln-queue.path 2>/dev/null || true
systemctl stop kiln-controller.service 2>/dev/null || true
systemctl disable --now kiln-network.service 2>/dev/null || true

if [[ -x /usr/local/libexec/kiln/network-teardown ]]; then
    /usr/local/libexec/kiln/network-teardown || true
fi

rm -f \
    /etc/systemd/system/kiln-controller.service \
    /etc/systemd/system/kiln-queue.path \
    /etc/systemd/system/kiln-network.service

systemctl daemon-reload

rm -f /usr/local/bin/kiln
rm -rf /usr/local/libexec/kiln
rm -rf /usr/local/share/kiln/web-src
rm -rf /var/lib/kiln/secret-staging

cat <<'EOF'

Kiln programs and services were removed.

Data was intentionally preserved:
  /srv/git
  /var/lib/kiln
  /etc/kiln

Users/groups were also preserved.
Remove those manually only if you are certain you no longer need the repositories,
build history or secrets.
EOF
