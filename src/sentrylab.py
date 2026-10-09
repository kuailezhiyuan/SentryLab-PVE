#!/usr/bin/python3
"""Temperature monitoring for PVE hosts; no shell configuration is executed."""

import argparse
import configparser
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import getpass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

VERSION = "1.1.0"
CONFIG_PATH = Path("/etc/sentrylab/sentrylab.conf")
STATE_PATH = Path("/var/lib/sentrylab")
CPU_DRIVERS = {"coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal"}
DISK_DRIVERS = {"nvme", "drivetemp"}
LOG = logging.getLogger("sentrylab")


def identifier(value):
    text = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return text[:48] or "pve"


def stable_key(prefix, identity, channel):
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{prefix}_{digest}_{identifier(channel)}"


def temperature(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 1) if math.isfinite(number) and -50 <= number <= 200 else None


@dataclass(frozen=True)
class Sensor:
    key: str
    name: str
    value: float
    source: str


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    broker: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    ca_file: str = ""
    host_id: str = ""
    topic_prefix: str = "sentrylab"
    discovery_prefix: str = "homeassistant"
    cpu: bool = True
    disks: bool = True
    smart: bool = False
    expire_after: int = 300

    @property
    def device_id(self):
        return "sentrylab_" + identifier(self.host_id or socket.gethostname())

    @property
    def broker_id(self):
        return f"{self.broker}:{self.port}:{int(self.tls)}"


def load_settings(path):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        found = parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise ValueError("Invalid INI configuration; check section and option syntax.") from exc
    if not found:
        raise ValueError(f"Configuration not found: {path}; run 'sudo sentrylab configure'.")
    try:
        settings = Settings(
            enabled=parser.getboolean("general", "enabled", fallback=False),
            broker=parser.get("mqtt", "broker", fallback="").strip(),
            port=parser.getint("mqtt", "port", fallback=1883),
            username=parser.get("mqtt", "username", fallback=""),
            password=parser.get("mqtt", "password", fallback=""),
            tls=parser.getboolean("mqtt", "tls", fallback=False),
            ca_file=parser.get("mqtt", "ca_file", fallback="").strip(),
            host_id=parser.get("general", "host_id", fallback="").strip(),
            topic_prefix=parser.get("mqtt", "topic_prefix", fallback="sentrylab").strip("/"),
            discovery_prefix=parser.get("mqtt", "discovery_prefix", fallback="homeassistant").strip("/"),
            cpu=parser.getboolean("sensors", "cpu", fallback=True),
            disks=parser.getboolean("sensors", "disks", fallback=True),
            smart=parser.getboolean("sensors", "smart", fallback=False),
            expire_after=parser.getint("general", "expire_after", fallback=300),
        )
    except ValueError as exc:
        raise ValueError("Invalid boolean or integer in configuration.") from exc
    if not 1 <= settings.port <= 65535 or settings.expire_after < 120:
        raise ValueError("MQTT port must be 1..65535 and expire_after must be at least 120 seconds.")
    for prefix in (settings.topic_prefix, settings.discovery_prefix):
        if not prefix or any(char in prefix for char in "+#\x00"):
            raise ValueError("MQTT topic prefixes must be nonempty and cannot contain wildcards.")
    return settings


def read_text(path, default=""):
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return default


def hwmon_identity(chip):
    # hwmon numbers can change after reboot. Prefer a hardware serial/device path.
    device = (chip / "device").resolve()
    serial = read_text(device / "serial")
    resolved = re.sub(r"/hwmon/hwmon\d+$", "", str(chip.resolve()))
    if not serial:
        serial = read_text(Path(resolved) / "serial")
    return serial or (str(device) if (chip / "device").exists() else resolved)


def collect_hwmon(settings, root=Path("/sys/class/hwmon")):
    sensors = []
    for chip in sorted(root.glob("hwmon*")):
        driver = read_text(chip / "name")
        if driver in CPU_DRIVERS and settings.cpu:
            kind, title = "cpu", "CPU"
        elif driver in DISK_DRIVERS and settings.disks:
            kind, title = "disk", "NVMe" if driver == "nvme" else "Disk"
        else:
            continue
        identity = hwmon_identity(chip)
        for value_path in sorted(chip.glob("temp*_input")):
            try:
                value = temperature(float(read_text(value_path)) / 1000)
            except ValueError:
                continue
            if value is None:
                continue
            channel = value_path.name.removesuffix("_input")
            label = read_text(chip / f"{channel}_label", channel)
            display_identity = identity if "/" not in identity else Path(identity).name
            name = f"{title} {display_identity} {label}"
            sensors.append(Sensor(stable_key(kind, identity, channel), name, value, str(value_path)))
    return sensors


def command_json(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


def collect_cpu_fallback():
    # Also accepts lm-sensors output on machines where a hwmon driver name differs.
    data = command_json(["sensors", "-j"])
    sensors = []
    if not isinstance(data, dict):
        return sensors
    for chip, features in data.items():
        if not any(chip.startswith(driver) for driver in CPU_DRIVERS) or not isinstance(features, dict):
            continue
        for label, readings in features.items():
            if not isinstance(readings, dict):
                continue
            for channel, raw in readings.items():
                if not re.fullmatch(r"temp\d+_input", channel):
                    continue
                value = temperature(raw)
                if value is not None:
                    sensors.append(Sensor(stable_key("cpu", chip, channel), f"CPU {chip} {label}", value, chip))
    return sensors


def smart_temperature(data):
    if not isinstance(data, dict):
        return None
    section = data.get("temperature", {})
    value = temperature(section.get("current")) if isinstance(section, dict) else None
    if value is not None:
        return value
    attributes = data.get("ata_smart_attributes", {})
    table = attributes.get("table", []) if isinstance(attributes, dict) else []
    for row in table:
        if row.get("id") in (190, 194):
            raw = row.get("raw", {})
            # The string starts with the current temperature; value can pack min/max bytes.
            match = re.match(r"^-?\d+(?:\.\d+)?", str(raw.get("string", "")))
            value = temperature(match.group() if match else raw.get("value"))
            if value is not None:
                return value
    return None


def collect_smart(root=Path("/sys/block")):
    if not shutil.which("smartctl"):
        LOG.warning("SMART is enabled but smartctl is absent; install smartmontools or disable SMART.")
        return []
    sensors = []
    for disk in sorted(root.glob("*")):
        if not re.fullmatch(r"(?:sd|hd)[a-z]+", disk.name):
            continue
        if read_text(disk / "device/type", "0") != "0":
            continue
        # Never request a self-test or a power-state change. Skip disks in standby.
        data = command_json(["smartctl", "-j", "-A", "-n", "standby,0", f"/dev/{disk.name}"])
        value = smart_temperature(data)
        if value is None:
            continue
        serial = str(data.get("serial_number") or disk.name)
        model = str(data.get("model_name") or disk.name)
        sensors.append(Sensor(stable_key("smart", serial, "temperature"), f"Disk {model} {serial}", value, f"/dev/{disk.name}"))
    return sensors


def collect(settings):
    sensors = collect_hwmon(settings)
    if settings.cpu and not any(item.key.startswith("cpu_") for item in sensors):
        sensors.extend(collect_cpu_fallback())
    if settings.disks and settings.smart:
        sensors.extend(collect_smart())
    return sensors


def discovery(settings, sensor):
    unique_id = f"{settings.device_id}_{sensor.key}"
    state_topic = f"{settings.topic_prefix}/{settings.device_id}/{sensor.key}/state"
    config_topic = f"{settings.discovery_prefix}/sensor/{settings.device_id}/{sensor.key}/config"
    payload = {
        "name": sensor.name,
        "unique_id": unique_id,
        "state_topic": state_topic,
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "state_class": "measurement",
        "expire_after": settings.expire_after,
        "qos": 1,
        "device": {
            "identifiers": [settings.device_id],
            "name": settings.host_id or socket.gethostname(),
            "manufacturer": "SentryLab",
            "model": "PVE temperature monitor",
            "sw_version": VERSION,
        },
    }
    return config_topic, state_topic, payload


class Publisher:
    def __init__(self, settings):
        import paho.mqtt.client as mqtt

        self.mqtt = mqtt
        kwargs = {"callback_api_version": mqtt.CallbackAPIVersion.VERSION2} if hasattr(mqtt, "CallbackAPIVersion") else {}
        self.client = mqtt.Client(client_id=f"{settings.device_id}-{os.getpid()}", **kwargs)
        self.connected = threading.Event()
        self.error = None
        self.client.on_connect = self.on_connect
        if settings.username:
            self.client.username_pw_set(settings.username, settings.password)
        if settings.tls:
            self.client.tls_set(ca_certs=settings.ca_file or None)
        self.settings = settings

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            self.error = f"MQTT connection rejected: {reason_code}"
        self.connected.set()

    def __enter__(self):
        self.client.connect_async(self.settings.broker, self.settings.port, keepalive=30)
        self.client.loop_start()
        if not self.connected.wait(10) or self.error:
            self.close()
            raise ConnectionError(self.error or "MQTT connection timed out; check broker, credentials and TLS.")
        return self

    def publish(self, topic, payload, retain):
        result = self.client.publish(topic, payload=payload, qos=1, retain=retain)
        if result.rc != self.mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError("MQTT publish failed.")
        result.wait_for_publish(timeout=10)
        if not result.is_published():
            raise ConnectionError("MQTT publish acknowledgment timed out.")

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()

    def __exit__(self, *args):
        self.close()


def atomic_write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sentrylab-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_manifest(state_path, settings):
    try:
        manifest = json.loads((state_path / "mqtt-topics.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(manifest, dict) or manifest.get("broker") != settings.broker_id:
        return []
    topics = manifest.get("topics", [])
    if not isinstance(topics, list):
        return []
    return [item for item in topics if isinstance(item, str) and item and not any(c in item for c in "+#\x00")]


def publish_sensors(settings, sensors, state_path=STATE_PATH):
    # Record before transmitting so an interrupted run can still be uninstalled cleanly.
    topics = set(read_manifest(state_path, settings))
    for sensor in sensors:
        config_topic, _, _ = discovery(settings, sensor)
        topics.add(config_topic)
    atomic_write(state_path / "mqtt-topics.json", json.dumps({"broker": settings.broker_id, "topics": sorted(topics)}) + "\n")
    with Publisher(settings) as publisher:
        for sensor in sensors:
            config_topic, state_topic, payload = discovery(settings, sensor)
            publisher.publish(config_topic, json.dumps(payload, ensure_ascii=False), retain=True)
            # Non-retained states let HA's expiry detect a stopped host reliably.
            publisher.publish(state_topic, str(sensor.value), retain=False)


def cleanup(settings, state_path=STATE_PATH):
    topics = read_manifest(state_path, settings)
    if not topics:
        return
    with Publisher(settings) as publisher:
        for topic in topics:
            publisher.publish(topic, "", retain=True)
    (state_path / "mqtt-topics.json").unlink(missing_ok=True)


@contextmanager
def state_lock(state_path=STATE_PATH):
    state_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_path / "monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another SentryLab operation is running; retry shortly.") from exc
        yield


def configure(path):
    if os.geteuid() != 0:
        raise ValueError("Run 'sudo sentrylab configure' to save configuration.")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    for section in ("general", "mqtt", "sensors"):
        if not parser.has_section(section):
            parser.add_section(section)
    old_broker = parser.get("mqtt", "broker", fallback="")
    broker = input(f"MQTT 地址 / broker [{old_broker}]: ").strip() or old_broker
    if not broker:
        raise ValueError("MQTT broker is required.")
    port = input(f"MQTT 端口 / port [{parser.get('mqtt', 'port', fallback='1883')}]: ").strip()
    username = input(f"MQTT 用户名 / username [{parser.get('mqtt', 'username', fallback='')}]: ").strip()
    password = getpass.getpass("MQTT 密码（输入隐藏，留空保留原值）: ")
    old_tls = parser.getboolean("mqtt", "tls", fallback=False)
    tls_text = input(f"启用 TLS？ / Enable TLS? [{'Y/n' if old_tls else 'y/N'}]: ").strip().lower()
    tls = tls_text in {"y", "yes"} if tls_text else old_tls
    parser.set("general", "enabled", "true")
    parser.set("mqtt", "broker", broker)
    parser.set("mqtt", "port", port or parser.get("mqtt", "port", fallback="1883"))
    parser.set("mqtt", "username", username or parser.get("mqtt", "username", fallback=""))
    parser.set("mqtt", "password", password or parser.get("mqtt", "password", fallback=""))
    parser.set("mqtt", "tls", str(tls).lower())
    from io import StringIO

    buffer = StringIO()
    parser.write(buffer)
    # Validate before replacing an existing, working configuration.
    fd, candidate = tempfile.mkstemp(prefix="sentrylab-config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(buffer.getvalue())
        load_settings(candidate)
    finally:
        os.unlink(candidate)
    atomic_write(path, buffer.getvalue())
    print(f"配置已保存 / Configuration saved: {path} (0600)")
    if Path("/run/systemd/system").exists():
        subprocess.run(["systemctl", "enable", "--now", "sentrylab-pve.timer"], check=True)
        subprocess.run(["systemctl", "start", "sentrylab-pve.service"], check=True)
    else:
        print("No running systemd; start the timer after boot: systemctl enable --now sentrylab-pve.timer")


def main(argv=None):
    parser = argparse.ArgumentParser(description="PVE temperature monitoring over MQTT")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("command", choices=("configure", "check", "run", "ready", "cleanup", "enable", "disable", "status"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.command in {"enable", "disable", "status"}:
            command = ["systemctl", args.command]
            if args.command != "status":
                command.append("--now")
            result = subprocess.run(command + ["sentrylab-pve.timer"], check=False)
            if args.command == "disable":
                subprocess.run(["systemctl", "stop", "sentrylab-pve.service"], check=False)
            return result.returncode
        if args.command == "configure":
            configure(args.config)
            return 0
        settings = load_settings(args.config)
        if args.command == "ready":
            return 0 if settings.enabled and settings.broker else 1
        if args.command == "check":
            sensors = collect(settings)
            print(json.dumps([vars(sensor) for sensor in sensors], ensure_ascii=False, indent=2))
            return 0 if sensors else 1
        if args.command == "run" and not settings.enabled:
            return 0
        if not settings.broker and args.command != "cleanup":
            raise ValueError("Configure the MQTT broker first: sudo sentrylab configure")
        with state_lock():
            if args.command == "cleanup":
                cleanup(settings)
            else:
                sensors = collect(settings)
                if not sensors:
                    raise ValueError("No readable temperature sensors; run 'sudo sentrylab check'.")
                publish_sensors(settings, sensors)
                LOG.info("Published %d temperature sensors.", len(sensors))
        return 0
    except (ValueError, OSError, RuntimeError, configparser.Error, subprocess.SubprocessError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
