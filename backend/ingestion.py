"""Hot-reconfigurable MQTT and serial ingestion workers."""
from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt
import serial
from serial import SerialException

from config_store import ConfigStore
from tracker import TransitTracker

LOG = logging.getLogger("aerotrack.ingestion")


class IngestionSupervisor:
    def __init__(self, store: ConfigStore, tracker: TransitTracker) -> None:
        self.store = store
        self.tracker = tracker
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        for name, target in (("aerotrack-mqtt", self._mqtt_worker), ("aerotrack-serial", self._serial_worker)):
            thread = threading.Thread(name=name, target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=3)

    def _mqtt_worker(self) -> None:
        active_signature: tuple[Any, ...] | None = None
        client: mqtt.Client | None = None
        while not self.stop_event.is_set():
            try:
                network = self.store.get()["network"]
                signature = (
                    network["mqttBrokerHost"], network["mqttBrokerPort"],
                    network["mqttTopicSighting"], network["mqttTopicHealthPrefix"],
                    network.get("mqttUseTls"), network.get("mqttUsernameEnvVar"), network.get("mqttPasswordEnvVar"),
                )
                if signature != active_signature:
                    if client is not None:
                        client.disconnect()
                        client.loop_stop()
                    active_signature = signature
                    client = self._new_mqtt_client(network)
                    client.connect_async(network["mqttBrokerHost"], int(network["mqttBrokerPort"]), keepalive=30)
                    client.loop_start()
                    self.tracker.set_transport("mqtt", False, f"connecting to {network['mqttBrokerHost']}:{network['mqttBrokerPort']}")
            except Exception as exc:  # keep retrying if broker/config is temporarily unavailable
                LOG.warning("MQTT setup failed: %s", exc)
                self.tracker.set_transport("mqtt", False, str(exc))
            self.stop_event.wait(2.0)
        if client is not None:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception:
                pass

    def _new_mqtt_client(self, network: dict[str, Any]) -> mqtt.Client:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="aerotrack-flask", clean_session=True)
        client.enable_logger(LOG)

        # HiveMQ Cloud / EMQX Cloud (and most private brokers) require TLS on
        # port 8883 plus a username/password. Actual credentials are never
        # stored in config.json — only the *names* of the environment
        # variables that hold them, same pattern as wifiPasswordEnvVar.
        if network.get("mqttUseTls"):
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)

        username_env = network.get("mqttUsernameEnvVar")
        password_env = network.get("mqttPasswordEnvVar")
        if username_env or password_env:
            username = os.environ.get(username_env) if username_env else None
            password = os.environ.get(password_env) if password_env else None
            if username_env and username is None:
                LOG.warning("MQTT username env var '%s' is not set", username_env)
            if password_env and password is None:
                LOG.warning("MQTT password env var '%s' is not set", password_env)
            client.username_pw_set(username, password)

        def on_connect(client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
            if reason_code == 0:
                client.subscribe(network["mqttTopicSighting"], qos=0)
                client.subscribe(f"{network['mqttTopicHealthPrefix']}+", qos=0)
                self.tracker.set_transport("mqtt", True, "subscribed")
            else:
                self.tracker.set_transport("mqtt", False, f"broker rejected connection: {reason_code}")

        def on_disconnect(client: mqtt.Client, userdata: Any, disconnect_flags: Any, reason_code: Any, properties: Any) -> None:
            if not self.stop_event.is_set():
                self.tracker.set_transport("mqtt", False, f"disconnected: {reason_code}")

        def on_message(client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
            try:
                payload = json.loads(message.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                with self.tracker.lock:
                    self.tracker._event("invalid", f"Dropped malformed MQTT JSON: {exc}", source="mqtt", topic=message.topic)
                return
            self.tracker.ingest_json(payload, "mqtt", message.topic)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        return client

    def _serial_worker(self) -> None:
        connection: serial.Serial | None = None
        active_signature: tuple[Any, ...] | None = None
        while not self.stop_event.is_set():
            try:
                serial_config = self.store.get()["serialFallback"]
                if not serial_config["enabled"]:
                    if connection is not None:
                        connection.close()
                        connection = None
                    active_signature = None
                    self.tracker.set_transport("serial", False, "disabled")
                    self.stop_event.wait(1.0)
                    continue

                signature = (serial_config["port"], serial_config["baudRate"])
                if connection is None or signature != active_signature:
                    if connection is not None:
                        connection.close()
                    connection = serial.Serial(serial_config["port"], int(serial_config["baudRate"]), timeout=1)
                    active_signature = signature
                    self.tracker.set_transport("serial", True, f"reading {signature[0]} at {signature[1]}")

                raw = connection.readline()
                if not raw:
                    continue
                try:
                    text = raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                # Gateway diagnostics share this UART. Only JSON-object lines are data.
                if not text.startswith("{"):
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    with self.tracker.lock:
                        self.tracker._event("invalid", f"Ignored malformed serial JSON: {exc}", source="serial")
                    continue
                self.tracker.ingest_json(payload, "serial")
            except (SerialException, OSError) as exc:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                connection = None
                active_signature = None
                self.tracker.set_transport("serial", False, str(exc))
                self.stop_event.wait(2.0)
            except Exception as exc:
                LOG.exception("Serial worker error")
                self.tracker.set_transport("serial", False, str(exc))
                self.stop_event.wait(2.0)
        if connection is not None:
            connection.close()
