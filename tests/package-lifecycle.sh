#!/bin/sh
# Run only inside a disposable Debian test container, never on a PVE host.
set -eu
if [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ]; then
    echo 'This destructive lifecycle test must run in a disposable test container.' >&2
    exit 1
fi
package=${1:?Pass the absolute path of the deb under test}
package_version=$(dpkg-deb --field "$package" Version)
canary=/etc/systemd/system/sentrylab-unrelated-test.service
mkdir -p /etc/systemd/system
printf '[Unit]\nDescription=Unrelated permission canary\n' > "$canary"
chmod 0600 "$canary"
apt-get install -y --no-install-recommends "$package"
test "$(sentrylab --version)" = "${package_version%%-*}"
test "$(stat -c %a /etc/sentrylab/sentrylab.conf)" = 600
test "$(stat -c %a "$canary")" = 600
sentrylab run
if sentrylab ready; then
    echo 'An unconfigured package must not publish MQTT' >&2
    exit 1
fi

# User credentials and deliberate timer disablement must survive an upgrade.
cat > /etc/sentrylab/sentrylab.conf <<'CONF'
[general]
enabled = false
[mqtt]
broker = 127.0.0.1
port = 1
password = upgrade-test-100% $(literal)
CONF
before=$(sha256sum /etc/sentrylab/sentrylab.conf)
systemctl disable sentrylab-pve.timer
upgrade_dir=$(mktemp -d)
dpkg-deb --raw-extract "$package" "$upgrade_dir/package"
sed -i "s/^Version:.*/Version: ${package_version}+lifecycle1/" "$upgrade_dir/package/DEBIAN/control"
dpkg-deb --root-owner-group --build "$upgrade_dir/package" "$upgrade_dir/upgrade.deb"
dpkg -i "$upgrade_dir/upgrade.deb"
test "$before" = "$(sha256sum /etc/sentrylab/sentrylab.conf)"
test ! -L /etc/systemd/system/timers.target.wants/sentrylab-pve.timer
test "$(stat -c %a "$canary")" = 600

# A failed MQTT cleanup must not prevent remove/purge or delete unrelated files.
mkdir -p /var/lib/sentrylab
printf '%s\n' '{"broker":"127.0.0.1:1:0","topics":["homeassistant/sensor/sentrylab_fixture/cpu/config"]}' > /var/lib/sentrylab/mqtt-topics.json
dpkg --remove sentrylab-pve
test -f /etc/sentrylab/sentrylab.conf
test ! -e /usr/bin/sentrylab
test "$before" = "$(sha256sum /etc/sentrylab/sentrylab.conf)"
dpkg --purge sentrylab-pve
test ! -e /etc/sentrylab/sentrylab.conf
test ! -e /var/lib/sentrylab
test ! -L /etc/systemd/system/timers.target.wants/sentrylab-pve.timer
test "$(stat -c %a "$canary")" = 600
rm -rf "$upgrade_dir"
printf '%s\n' 'Package install/upgrade/remove/purge checks passed.'
