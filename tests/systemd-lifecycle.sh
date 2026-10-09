#!/bin/sh
# Exercise real systemd activation and MQTT cleanup in a disposable container.
set -eu
test -f /.dockerenv
test "$(cat /proc/1/comm)" = systemd
# Official Debian Docker images suppress maintainer-script service starts.
# Remove that container-only policy to test normal host behavior.
rm -f /usr/sbin/policy-rc.d
package=${1:?Pass the absolute path of the deb under test}
mkdir -p /etc/systemd/system
printf '[Unit]\nDescription=Unrelated live systemd canary\n' > /etc/systemd/system/unrelated-canary.service
chmod 0600 /etc/systemd/system/unrelated-canary.service
apt-get install -y --no-install-recommends "$package"
systemctl is-enabled sentrylab-pve.timer
systemctl is-active sentrylab-pve.timer
test "$(stat -c %a /etc/systemd/system/unrelated-canary.service)" = 600

# Mock lm-sensors data rather than touching real hardware in the test VM.
cat > /usr/local/bin/sensors <<'SENSORS'
#!/bin/sh
printf '%s\n' '{"coretemp-isa-0001":{"Package id 1":{"temp1_input":52.5}},"k10temp-pci-00c3":{"Tdie":{"temp2_input":61.0}}}'
SENSORS
chmod 0755 /usr/local/bin/sensors
cat > /etc/sentrylab/sentrylab.conf <<'CONF'
[general]
enabled = true
host_id = live-systemd-fixture
[mqtt]
broker = 127.0.0.1
port = 1883
[sensors]
disks = false
smart = false
CONF
chmod 0600 /etc/sentrylab/sentrylab.conf
systemctl start mosquitto.service
systemctl start sentrylab-pve.service
test "$(systemctl show sentrylab-pve.service -p Result --value)" = success
mosquitto_sub -h 127.0.0.1 -t 'homeassistant/sensor/sentrylab_live_systemd_fixture/+/config' -C 2 -W 5 > /tmp/discovery.json
python3 -c 'import json; records=[json.loads(line) for line in open("/tmp/discovery.json")]; assert len(records)==2; assert all(item["device_class"]=="temperature" for item in records)'

dpkg --remove sentrylab-pve
test -f /etc/sentrylab/sentrylab.conf
if systemctl is-active --quiet sentrylab-pve.timer; then
    echo 'Timer must be stopped during remove' >&2
    exit 1
fi
if mosquitto_sub -h 127.0.0.1 -t 'homeassistant/sensor/sentrylab_live_systemd_fixture/+/config' -C 1 -W 2 > /tmp/after-remove.json 2>/dev/null; then
    echo 'Retained discovery must be deleted during remove' >&2
    exit 1
fi
test ! -s /tmp/after-remove.json
dpkg --purge sentrylab-pve
test ! -e /etc/sentrylab/sentrylab.conf
test ! -e /var/lib/sentrylab
test "$(stat -c %a /etc/systemd/system/unrelated-canary.service)" = 600
printf '%s\n' 'Real systemd and retained MQTT cleanup checks passed.'
