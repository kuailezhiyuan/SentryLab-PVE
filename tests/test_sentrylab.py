import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import sentrylab as app


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def chip(self, number, driver, values, serial=""):
        path = self.root / f"hwmon{number}"
        path.mkdir()
        (path / "name").write_text(driver)
        device = self.root / f"device-{driver}-{serial or number}"
        device.mkdir(exist_ok=True)
        (path / "device").symlink_to(device)
        if serial:
            (device / "serial").write_text(serial)
        for channel, (raw, label) in values.items():
            (path / f"{channel}_input").write_text(str(raw))
            (path / f"{channel}_label").write_text(label)
        return path

    def test_intel_packages_and_amd_tdie(self):
        self.chip(0, "coretemp", {"temp1": (52500, "Package id 0"), "temp2": (50000, "Core 0")})
        self.chip(1, "k10temp", {"temp1": (61500, "Tctl"), "temp2": (60000, "Tdie")})
        sensors = app.collect_hwmon(app.Settings(), self.root)
        self.assertEqual({sensor.value for sensor in sensors}, {52.5, 50.0, 61.5, 60.0})
        self.assertEqual(len({sensor.key for sensor in sensors}), 4)

    def test_nvme_id_survives_hwmon_renumbering(self):
        chip = self.chip(3, "nvme", {"temp1": (42000, "Composite")}, "SERIAL-001")
        first = app.collect_hwmon(app.Settings(), self.root)[0]
        chip.rename(self.root / "hwmon9")
        second = app.collect_hwmon(app.Settings(), self.root)[0]
        self.assertEqual(first.key, second.key)
        self.assertEqual(second.value, 42.0)

    def test_non_nvme_disk_hwmon(self):
        self.chip(2, "drivetemp", {"temp1": (35000, "Drive")}, "SATA-1")
        self.assertEqual(app.collect_hwmon(app.Settings(cpu=False), self.root)[0].value, 35.0)

    def test_flags_and_invalid_readings(self):
        self.chip(0, "coretemp", {"temp1": ("bad", "Broken"), "temp2": (9999999, "Invalid")})
        self.chip(1, "nvme", {"temp1": (42000, "Composite")})
        self.assertEqual(app.collect_hwmon(app.Settings(cpu=False, disks=False), self.root), [])
        self.assertEqual(len(app.collect_hwmon(app.Settings(), self.root)), 1)
        for value in ("nan", "inf", None, 999):
            self.assertIsNone(app.temperature(value))

    def test_lm_sensors_fallback_handles_all_cpu_chips(self):
        data = {
            "coretemp-isa-0001": {"Package id 1": {"temp1_input": 55}},
            "k10temp-pci-00c3": {"Tdie": {"temp2_input": 61}},
            "nvme-pci-0300": {"Composite": {"temp1_input": 40}},
        }
        with patch.object(app, "command_json", return_value=data):
            self.assertEqual({sensor.value for sensor in app.collect_cpu_fallback()}, {55.0, 61.0})

    def test_smart_skips_standby_and_never_requests_self_tests(self):
        (self.root / "sda/device").mkdir(parents=True)
        (self.root / "sda/device/type").write_text("0")
        (self.root / "nvme0n1").mkdir()
        with patch.object(app.shutil, "which", return_value="/usr/sbin/smartctl"), patch.object(app, "command_json", return_value={"power_mode": "STANDBY"}) as run:
            self.assertEqual(app.collect_smart(self.root), [])
            run.assert_called_once_with(["smartctl", "-j", "-A", "-n", "standby,0", "/dev/sda"])

    def test_smart_current_temperature_and_packed_raw_attribute(self):
        self.assertEqual(app.smart_temperature({"temperature": {"current": 37}}), 37.0)
        data = {"ata_smart_attributes": {"table": [{"id": 194, "raw": {"value": 987654, "string": "36 (Min/Max 20/48)"}}]}}
        self.assertEqual(app.smart_temperature(data), 36.0)

    def test_smart_absent_is_optional(self):
        with patch.object(app.shutil, "which", return_value=None):
            with self.assertLogs("sentrylab", level="WARNING"):
                self.assertEqual(app.collect_smart(self.root), [])


class ConfigAndStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.config = self.root / "sentrylab.conf"

    def config_text(self, extra=""):
        self.config.write_text("[general]\nenabled = false\n[mqtt]\nbroker = localhost\n" + extra)

    def test_password_is_literal_and_anonymous_mqtt_is_valid(self):
        self.config_text("password = 100% $(touch /tmp/not-executed)\n")
        settings = app.load_settings(self.config)
        self.assertEqual(settings.password, "100% $(touch /tmp/not-executed)")
        self.assertEqual(settings.username, "")
        self.assertFalse(settings.enabled)

    def test_unconfigured_service_condition_skips_without_publishing(self):
        self.config_text()
        with patch.object(app, "Publisher") as publish:
            self.assertEqual(app.main(["--config", str(self.config), "ready"]), 1)
            self.assertEqual(app.main(["--config", str(self.config), "run"]), 0)
            publish.assert_not_called()

    def test_invalid_port_and_topic_wildcards_rejected(self):
        for text in ("port = 70000\n", "port = abc\n", "topic_prefix = +/host\n", "discovery_prefix = homeassistant/#\n"):
            self.config_text(text)
            with self.assertRaises(ValueError):
                app.load_settings(self.config)

    def test_atomic_secret_and_manifest_files_are_private(self):
        path = self.root / "state/secret.conf"
        app.atomic_write(path, "secret")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        app.atomic_write(path, "replacement")
        self.assertEqual(path.read_text(), "replacement")

    def test_sensor_topics_do_not_overwrite_cpu_with_nvme(self):
        settings = app.Settings(host_id="pve", discovery_prefix="ha-custom")
        cpu = app.discovery(settings, app.Sensor("cpu_1", "CPU", 52, "cpu"))
        disk = app.discovery(settings, app.Sensor("disk_1", "NVMe", 42, "nvme"))
        self.assertNotEqual(cpu[1], disk[1])
        self.assertTrue(cpu[0].startswith("ha-custom/sensor/"))
        self.assertEqual(cpu[2]["expire_after"], 300)
        self.assertEqual(cpu[2]["device_class"], "temperature")
        self.assertNotEqual(cpu[2]["unique_id"], disk[2]["unique_id"])

    def test_cleanup_only_uses_its_recorded_broker_and_topics(self):
        settings = app.Settings(broker="localhost")
        sensor = app.Sensor("cpu_1", "CPU", 52, "cpu")
        with patch.object(app, "Publisher") as publisher:
            app.publish_sensors(settings, [sensor], self.root)
            calls = publisher.return_value.__enter__.return_value.publish.call_args_list
            self.assertTrue(calls[0].kwargs["retain"])
            self.assertFalse(calls[1].kwargs["retain"])
        with patch.object(app, "Publisher") as publisher:
            app.cleanup(app.Settings(broker="other-broker"), self.root)
            publisher.assert_not_called()
        topics = app.read_manifest(self.root, settings)
        with patch.object(app, "Publisher") as publisher:
            app.cleanup(settings, self.root)
            publisher.return_value.__enter__.return_value.publish.assert_called_once_with(topics[0], "", retain=True)
        self.assertFalse((self.root / "mqtt-topics.json").exists())

    def test_interrupted_publish_still_records_cleanup_topics(self):
        settings = app.Settings(broker="localhost")
        with patch.object(app, "Publisher") as publisher:
            publisher.return_value.__enter__.side_effect = ConnectionError("offline")
            with self.assertRaises(ConnectionError):
                app.publish_sensors(settings, [app.Sensor("cpu_1", "CPU", 52, "cpu")], self.root)
        self.assertEqual(len(app.read_manifest(self.root, settings)), 1)

    def test_corrupt_manifest_is_ignored(self):
        (self.root / "mqtt-topics.json").write_text("invalid")
        self.assertEqual(app.read_manifest(self.root, app.Settings()), [])
        (self.root / "mqtt-topics.json").write_text('{"broker":":1883:0","topics":null}')
        self.assertEqual(app.read_manifest(self.root, app.Settings()), [])

    def test_unconfigured_cleanup_succeeds_without_connecting(self):
        self.config.write_text("[general]\nenabled = false\n[mqtt]\nbroker =\n")
        with patch.object(app, "state_lock"), patch.object(app, "cleanup") as cleanup:
            self.assertEqual(app.main(["--config", str(self.config), "cleanup"]), 0)
            cleanup.assert_called_once()

    def test_concurrent_runs_cannot_modify_manifest(self):
        with app.state_lock(self.root):
            with self.assertRaises(RuntimeError):
                with app.state_lock(self.root):
                    self.fail("Lock should not be reentered")


@unittest.skipUnless(shutil.which("mosquitto"), "Live MQTT test requires the mosquitto test broker")
class LiveMqttTests(unittest.TestCase):
    def test_discovery_is_retained_states_are_fresh_and_cleanup_deletes_entities(self):
        import paho.mqtt.client as mqtt

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            broker_config = root / "mosquitto.conf"
            broker_config.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\nuser root\n")
            broker = subprocess.Popen(["mosquitto", "-c", str(broker_config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            clients = []
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Test broker did not start")

                def subscriber():
                    kwargs = {"callback_api_version": mqtt.CallbackAPIVersion.VERSION2} if hasattr(mqtt, "CallbackAPIVersion") else {}
                    client = mqtt.Client(**kwargs)
                    messages = queue.Queue()
                    subscribed = threading.Event()
                    client.on_connect = lambda c, *args: c.subscribe("#", qos=1)
                    client.on_subscribe = lambda *args: subscribed.set()
                    client.on_message = lambda c, u, message: messages.put(message)
                    client.connect("127.0.0.1", port)
                    client.loop_start()
                    clients.append(client)
                    self.assertTrue(subscribed.wait(5))
                    return messages

                messages = subscriber()
                settings = app.Settings(broker="127.0.0.1", port=port, host_id="fixture-pve")
                sensors = [app.Sensor("cpu_fixture", "CPU", 52.5, "fixture"), app.Sensor("disk_fixture", "NVMe", 42.0, "fixture")]
                app.publish_sensors(settings, sensors, root)
                received = [messages.get(timeout=5) for _ in range(4)]
                states = {message.topic: message.payload.decode() for message in received if message.topic.endswith("/state")}
                self.assertEqual(set(states.values()), {"52.5", "42.0"})
                late_messages = subscriber()
                retained = [late_messages.get(timeout=5) for _ in range(2)]
                self.assertTrue(all(message.retain and message.topic.endswith("/config") for message in retained))
                self.assertEqual({json.loads(message.payload)["device_class"] for message in retained}, {"temperature"})
                with self.assertRaises(queue.Empty):
                    late_messages.get(timeout=0.2)
                app.cleanup(settings, root)
                fresh_messages = subscriber()
                with self.assertRaises(queue.Empty):
                    fresh_messages.get(timeout=0.2)
            finally:
                for client in clients:
                    client.disconnect()
                    client.loop_stop()
                broker.terminate()
                broker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
