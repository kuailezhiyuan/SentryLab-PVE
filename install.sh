#!/bin/sh
# Install a built/downloaded Debian package through apt.
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root: sudo sh install.sh /path/to/sentrylab-pve.deb" >&2
    exit 1
fi
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$#" -eq 1 ]; then
    package=$1
else
    set -- "$project_dir"/dist/sentrylab-pve_*_all.deb
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
        echo "Download the deb from GitHub Releases, or run tools/build-deb.sh first." >&2
        exit 1
    fi
    package=$1
fi
if [ ! -f "$package" ]; then
    echo "Debian package not found." >&2
    exit 1
fi
case "$package" in
    /*) ;;
    *) package="$(pwd)/$package" ;;
esac
exec apt-get install -y -- "$package"
