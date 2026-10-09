#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM
mkdir -p "$build_dir/sentrylab" "$project_dir/dist"
tar -C "$project_dir" --exclude=.git --exclude=dist --exclude='__pycache__' \
    --exclude='debian/.debhelper' --exclude='debian/sentrylab-pve' -cf - . | \
    tar -C "$build_dir/sentrylab" -xf -
cd "$build_dir/sentrylab"
dpkg-buildpackage --build=binary --no-sign
cp "$build_dir"/*.deb "$project_dir/dist/"
cd "$project_dir/dist"
sha256sum ./*.deb > SHA256SUMS
printf 'Package and checksums: %s/dist/\n' "$project_dir"
