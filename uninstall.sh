#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${EUID}" -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

systemctl disable --now kilnr-queue.path 2>/dev/null || true
systemctl stop kilnr-controller.service 2>/dev/null || true
systemctl disable --now kilnr-network.service 2>/dev/null || true

if [[ -x /usr/local/libexec/kilnr/network-teardown ]]; then
    /usr/local/libexec/kilnr/network-teardown || true
fi

rm -f \
    /etc/systemd/system/kilnr-controller.service \
    /etc/systemd/system/kilnr-queue.path \
    /etc/systemd/system/kilnr-network.service

systemctl daemon-reload

rm -f /usr/local/bin/kilnr
rm -rf /usr/local/libexec/kilnr
rm -rf /usr/local/share/kilnr/web-src
rm -rf /var/lib/kilnr/secret-staging

cat <<'EOF'

Kilnr programs and services were removed.

Data was intentionally preserved:
  /srv/git
  /var/lib/kilnr
  /etc/kilnr

Users/groups were also preserved.
Remove those manually only if you are certain you no longer need the repositories,
build history or secrets.
EOF
