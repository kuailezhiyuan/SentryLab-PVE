#!/bin/sh
# Standard remove preserves configuration; --purge explicitly removes it.
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo sh uninstall.sh [--purge]" >&2
    exit 1
fi
case "${1:-}" in
    "") exec apt-get remove -y sentrylab-pve ;;
    --purge) exec apt-get purge -y sentrylab-pve ;;
    *) echo "Usage: sudo sh uninstall.sh [--purge]" >&2; exit 1 ;;
esac
