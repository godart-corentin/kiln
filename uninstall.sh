#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PURGE=0
ASSUME_YES=0

usage() {
    cat <<'USAGE'
Usage: sudo ./uninstall.sh [--purge [--yes]]

Without options, remove Kilnr programs and services while preserving data.

  --purge  Also remove repositories, build history, configuration, secrets,
           optional web data, and Kilnr system users/groups.
  --yes    Skip the PURGE confirmation. Valid only with --purge.
USAGE
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --purge) PURGE=1 ;;
        --yes) ASSUME_YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

if [[ "$ASSUME_YES" -eq 1 && "$PURGE" -ne 1 ]]; then
    echo "uninstall: --yes requires --purge" >&2
    exit 2
fi

[[ "${EUID}" -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

if [[ "$PURGE" -eq 1 && "$ASSUME_YES" -ne 1 ]]; then
    cat >&2 <<'WARNING'
WARNING: this permanently deletes all Kilnr data, including:
  /srv/git       (all hosted Git repositories)
  /var/lib/kilnr (build history and runtime data)
  /etc/kilnr     (configuration and secrets)
  /opt/kilnr     (optional web deployment and backups)
WARNING
    printf 'Type PURGE to continue: ' >&2
    read -r confirmation
    if [[ "$confirmation" != "PURGE" ]]; then
        echo "Purge cancelled." >&2
        exit 1
    fi
fi

if [[ "$PURGE" -eq 1 && -x "$ROOT_DIR/uninstall-web.sh" ]] \
    && { [[ -d /opt/kilnr ]] || [[ -f /etc/kilnr/web.json ]]; }
then
    "$ROOT_DIR/uninstall-web.sh"
fi

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

if [[ "$PURGE" -eq 1 ]]; then
    rm -rf \
        /usr/local/share/kilnr \
        /opt/kilnr \
        /var/lib/kilnr \
        /etc/kilnr \
        /srv/git

    for user in kilnr-web kilnr; do
        if getent passwd "$user" >/dev/null; then
            userdel "$user"
        fi
    done

    for group in kilnr-readers kilnr-submit kilnr-web kilnr; do
        if getent group "$group" >/dev/null; then
            groupdel "$group"
        fi
    done

    cat <<'PURGED'

Kilnr programs, services, data, repositories, configuration, secrets, and
system identities were permanently removed.
PURGED
else
    cat <<'PRESERVED'

Kilnr programs and services were removed.

Data was intentionally preserved:
  /srv/git
  /var/lib/kilnr
  /etc/kilnr

Users/groups were also preserved.
Remove those manually only if you are certain you no longer need the repositories,
build history or secrets.
PRESERVED
fi
