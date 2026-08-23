#!/usr/bin/env python3
"""Publish hardware-shaped AeroTrack data to MQTT without ESP32 boards."""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

PROJECT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config_store import ConfigStore  # noqa: E402


def haversine_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    from math import atan2, cos, radians, sin, sqrt
    radius = 6371.0088
    lat1, lat2 = radians(a["lat"]), radians(b["lat"])
    dlat, dlon = lat2 - lat1, radians(b["lon"] - a["lon"])
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return radius * 2 * atan2(sqrt(h), sqrt(max(0.0, 1 - h)))


class ConfigReader:
    """Reload settings when either config.json or the optional SQLite DB changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.store = ConfigStore(path, PROJECT_DIR / "schemas" / "config.schema.json")
        self.signature: tuple[tuple[int, int], tuple[int, int]] | None = None
        self.value: dict[str, Any] = {}

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int]:
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return 0, 0

    def get(self) -> dict[str, Any]:
        signature = (self._stamp(self.path), self._stamp(self.store.database_path))
        if signature != self.signature:
            self.value = self.store.get()
            self.signature = signature
            source = self.store.storage_status()["mode"]
            print(f"[config] loaded {source} settings")
        return self.value


class Publisher:
    def __init__(self) -> None:
        self.client: mqtt.Client | None = None
        self.signature: tuple[str, int] | None = None
        self.connected = False

    def ensure(self, config: dict[str, Any]) -> None:
        network = config["network"]
        signature = (network["mqttBrokerHost"], int(network["mqttBrokerPort"]))
        if signature == self.signature and self.client is not None:
            return
        self.close()
        self.signature = signature
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"aerotrack-simulator-{random.randrange(10000):04d}")
        self.client.on_connect = lambda client, userdata, flags, reason_code, properties: self._on_connect(reason_code)
        self.client.on_disconnect = lambda client, userdata, flags, reason_code, properties: self._on_disconnect(reason_code)
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.connect_async(*signature, keepalive=30)
        self.client.loop_start()
        print(f"[mqtt] connecting to {signature[0]}:{signature[1]}")

    def _on_connect(self, reason_code: Any) -> None:
        self.connected = reason_code == 0
        print("[mqtt] connected" if self.connected else f"[mqtt] connection rejected: {reason_code}")

    def _on_disconnect(self, reason_code: Any) -> None:
        self.connected = False
        print(f"[mqtt] disconnected: {reason_code}")

    def publish(self, topic: str, payload: dict[str, Any], duplicate: bool = False) -> None:
        line = json.dumps(payload, separators=(",", ":"))
        if self.client is None:
            print(f"[drop] no MQTT client: {topic} {line}")
            return
        info = self.client.publish(topic, line, qos=0, retain=False)
        label = "duplicate" if duplicate else "publish"
        print(f"[{label}] {topic} {line} (rc={info.rc})")

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
                self.client.loop_stop()
            except Exception:
                pass
        self.client = None
        self.connected = False


class Simulator:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.reader = ConfigReader(Path(args.config))
        self.publisher = Publisher()
        self.killed = set(args.kill_stop or [])
        self.killed.difference_update(args.revive_stop or [])
        self.lock = threading.Lock()
        self.bus_state: dict[str, dict[str, Any]] = {}
        self.next_health = 0.0
        self.stop_event = threading.Event()

    def command_worker(self) -> None:
        if sys.stdin is None:
            return
        for raw in sys.stdin:
            parts = raw.strip().split(maxsplit=1)
            if not parts:
                continue
            if parts[0].lower() in {"quit", "exit"}:
                self.stop_event.set()
                return
            if len(parts) != 2 or parts[0].lower() not in {"kill", "revive"}:
                print("[command] use: kill STOP-A | revive STOP-A | quit")
                continue
            stop_id = parts[1].strip()
            with self.lock:
                if parts[0].lower() == "kill":
                    self.killed.add(stop_id)
                else:
                    self.killed.discard(stop_id)
            print(f"[command] {stop_id} is now {'silent' if parts[0].lower() == 'kill' else 'active'}")

    def is_killed(self, stop_id: str) -> bool:
        with self.lock:
            return stop_id in self.killed

    def link_mode(self, stop: dict[str, Any]) -> str:
        if self.args.mode != "alternate":
            return self.args.mode
        return "rs485" if int(stop["sequence"]) % 2 == 0 else "espnow_direct"

    def publish_health(self, config: dict[str, Any]) -> None:
        prefix = config["network"]["mqttTopicHealthPrefix"]
        for stop in config["route"]["stops"]:
            if self.is_killed(stop["id"]):
                continue
            payload = {
                "schemaVersion": "1.0", "stopId": stop["id"],
                "uptimeSec": int(time.monotonic()), "linkMode": self.link_mode(stop),
                "rssiFloorNoise": random.randint(-101, -88), "timestampMs": int(time.time() * 1000),
            }
            self.publisher.publish(f"{prefix}{stop['id']}", payload)

    def publish_sighting(self, config: dict[str, Any], bus: dict[str, Any], stop: dict[str, Any], seq: int) -> None:
        if self.is_killed(stop["id"]):
            print(f"[silent] {stop['id']} killed; skipped {bus['id']} seq={seq}")
            return
        payload = {
            "schemaVersion": "1.0", "stopId": stop["id"], "busId": bus["id"],
            "rssi": random.randint(-80, -50), "seq": seq, "gatewayReceivedAtMs": int(time.time() * 1000),
        }
        topic = config["network"]["mqttTopicSighting"]
        self.publisher.publish(topic, payload)
        if random.random() < self.args.dup_rate:
            time.sleep(min(0.2, 0.5 / self.args.speed_multiplier))
            self.publisher.publish(topic, payload, duplicate=True)

    def update_bus(self, config: dict[str, Any], bus: dict[str, Any], now: float) -> None:
        stops = sorted(config["route"]["stops"], key=lambda item: item["sequence"])
        if not stops:
            return
        state = self.bus_state.setdefault(bus["id"], {"stopId": stops[0]["id"], "direction": 1, "seq": 0, "due": 0.0})
        if now < state["due"]:
            return
        index_by_id = {stop["id"]: index for index, stop in enumerate(stops)}
        index = index_by_id.get(state["stopId"], 0)
        current = stops[index]
        state["seq"] += 1
        self.publish_sighting(config, bus, current, state["seq"])
        if len(stops) == 1:
            state["due"] = now + 10 / self.args.speed_multiplier
            return

        direction = state["direction"]
        candidate = index + direction
        if candidate >= len(stops) or candidate < 0:
            if config["route"].get("loop", False):
                candidate = 0 if direction > 0 else len(stops) - 1
            else:
                direction *= -1
                state["direction"] = direction
                candidate = index + direction
        following = stops[candidate]
        speed = max(0.1, float(config["eta"]["assumedSpeedKmph"]))
        travel_real_seconds = haversine_km(current, following) / speed * 3600.0
        state["stopId"] = following["id"]
        state["due"] = now + max(0.25, travel_real_seconds / self.args.speed_multiplier)
        print(f"[move] {bus['id']} {current['id']} -> {following['id']} in {state['due']-now:.1f}s")

    def run(self) -> None:
        command_thread = threading.Thread(target=self.command_worker, name="simulator-commands", daemon=True)
        command_thread.start()
        print("[ready] commands: kill STOP-A | revive STOP-A | quit")
        try:
            while not self.stop_event.is_set():
                try:
                    config = self.reader.get()
                    self.publisher.ensure(config)
                    now = time.monotonic()
                    for bus in config["route"]["buses"]:
                        self.update_bus(config, bus, now)
                    if now >= self.next_health:
                        self.publish_health(config)
                        self.next_health = now + 30.0 / self.args.speed_multiplier
                except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    print(f"[error] {exc}; retrying")
                self.stop_event.wait(0.1)
        except KeyboardInterrupt:
            print("\n[stop] interrupted")
        finally:
            self.publisher.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate AeroTrack bus sightings and stop health over MQTT")
    parser.add_argument("--config", default=str(PROJECT_DIR / "config.json"), help="path to config.json")
    parser.add_argument("--speed-multiplier", type=float, default=1.0, help="run travel and heartbeat clocks faster")
    parser.add_argument("--dup-rate", type=float, default=0.0, help="probability (0..1) of publishing an identical sighting twice")
    parser.add_argument("--kill-stop", action="append", default=[], metavar="STOP_ID", help="start with this stop silent; repeatable")
    parser.add_argument("--revive-stop", action="append", default=[], metavar="STOP_ID", help="remove a stop from the initial silent set")
    parser.add_argument("--mode", choices=["alternate", "rs485", "uart_direct", "espnow_direct"], default="alternate", help="health link-mode badges")
    args = parser.parse_args()
    if args.speed_multiplier <= 0:
        parser.error("--speed-multiplier must be greater than zero")
    if not 0 <= args.dup_rate <= 1:
        parser.error("--dup-rate must be between 0 and 1")
    return args


if __name__ == "__main__":
    Simulator(parse_args()).run()
