"""In-memory transit state, de-duplication, health and ETA calculations."""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from config_store import ConfigStore


def now_ms() -> int:
    return int(time.time() * 1000)


def haversine_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    radius_km = 6371.0088
    lat1, lat2 = math.radians(float(a["lat"])), math.radians(float(b["lat"]))
    dlat = lat2 - lat1
    dlon = math.radians(float(b["lon"]) - float(a["lon"]))
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return radius_km * 2 * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1 - h)))


class TransitTracker:
    def __init__(self, config_store: ConfigStore, schemas_dir: Path) -> None:
        self.config_store = config_store
        self.lock = threading.RLock()
        self.buses: dict[str, dict[str, Any]] = {}
        self.stops: dict[str, dict[str, Any]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self.last_ingest_ms: int | None = None
        self.transports = {
            "mqtt": {"connected": False, "detail": "starting"},
            "serial": {"connected": False, "detail": "disabled"},
        }
        self._sighting_validator = self._load_validator(Path(schemas_dir) / "sighting.schema.json")
        self._health_validator = self._load_validator(Path(schemas_dir) / "stop_health.schema.json")

    @staticmethod
    def _load_validator(path: Path) -> Draft202012Validator:
        with path.open("r", encoding="utf-8") as handle:
            return Draft202012Validator(json.load(handle))

    def _event(self, kind: str, message: str, **details: Any) -> None:
        event = {"timestampMs": now_ms(), "kind": kind, "message": message}
        event.update(details)
        self.events.appendleft(event)

    @staticmethod
    def _validation_message(validator: Draft202012Validator, payload: Any) -> str | None:
        errors = sorted(validator.iter_errors(payload), key=lambda e: (list(e.absolute_path), e.message))
        if not errors:
            return None
        error = errors[0]
        path = ".".join(str(p) for p in error.absolute_path) or "payload"
        return f"{path}: {error.message}"

    def ingest_json(self, payload: Any, source: str, topic: str = "") -> bool:
        if not isinstance(payload, dict):
            with self.lock:
                self._event("invalid", "Dropped non-object payload", source=source, topic=topic)
            return False
        if {"busId", "stopId", "seq"}.issubset(payload):
            return self.process_sighting(payload, source, topic)
        if {"stopId", "uptimeSec", "linkMode"}.issubset(payload):
            return self.process_health(payload, source, topic)
        with self.lock:
            self._event("invalid", "Dropped unrecognized JSON payload", source=source, topic=topic)
        return False

    def process_sighting(self, payload: dict[str, Any], source: str = "unknown", topic: str = "") -> bool:
        validation_error = self._validation_message(self._sighting_validator, payload)
        if validation_error:
            with self.lock:
                self._event("invalid", f"Dropped malformed sighting: {validation_error}", source=source, topic=topic)
            return False

        bus_id, stop_id, seq = payload["busId"], payload["stopId"], int(payload["seq"])
        with self.lock:
            current = self.buses.get(bus_id)
            if current is not None and seq <= current["lastSeq"]:
                self._event(
                    "dropped",
                    f"Dropped duplicate/out-of-order {bus_id} seq={seq}",
                    source=source,
                    busId=bus_id,
                    stopId=stop_id,
                    seq=seq,
                    lastAcceptedSeq=current["lastSeq"],
                )
                return False

            config = self.config_store.get()
            sequence_by_id = {stop["id"]: stop["sequence"] for stop in config["route"]["stops"]}
            if current is None:
                current = {
                    "lastStopId": stop_id,
                    "prevStopId": None,
                    "direction": 1,
                    "lastSeenMs": int(payload["gatewayReceivedAtMs"]),
                    "lastRssi": int(payload["rssi"]),
                    "lastSeq": seq,
                }
                self.buses[bus_id] = current
            else:
                old_stop = current["lastStopId"]
                if stop_id != old_stop:
                    current["prevStopId"] = old_stop
                    current["lastStopId"] = stop_id
                    old_seq, new_seq = sequence_by_id.get(old_stop), sequence_by_id.get(stop_id)
                    if old_seq is not None and new_seq is not None and new_seq != old_seq:
                        current["direction"] = 1 if new_seq > old_seq else -1
                current["lastSeenMs"] = int(payload["gatewayReceivedAtMs"])
                current["lastRssi"] = int(payload["rssi"])
                current["lastSeq"] = seq

            self.last_ingest_ms = now_ms()
            self._event(
                "sighting",
                f"Accepted {bus_id} at {stop_id}",
                source=source,
                busId=bus_id,
                stopId=stop_id,
                seq=seq,
                rssi=int(payload["rssi"]),
            )
            return True

    def process_health(self, payload: dict[str, Any], source: str = "unknown", topic: str = "") -> bool:
        validation_error = self._validation_message(self._health_validator, payload)
        if validation_error:
            with self.lock:
                self._event("invalid", f"Dropped malformed health: {validation_error}", source=source, topic=topic)
            return False
        stop_id = payload["stopId"]
        received = now_ms()
        with self.lock:
            self.stops[stop_id] = {
                "lastHeartbeatMs": received,
                "reportedTimestampMs": int(payload["timestampMs"]),
                "uptimeSec": int(payload["uptimeSec"]),
                "linkMode": payload["linkMode"],
                "rssiFloorNoise": payload.get("rssiFloorNoise"),
            }
            self.last_ingest_ms = received
            self._event("health", f"Heartbeat from {stop_id}", source=source, stopId=stop_id, linkMode=payload["linkMode"])
        return True

    def set_transport(self, name: str, connected: bool, detail: str) -> None:
        with self.lock:
            previous = self.transports.get(name, {}).get("connected")
            self.transports[name] = {"connected": connected, "detail": detail}
            if previous != connected:
                self._event("transport", f"{name} {'connected' if connected else 'disconnected'}", detail=detail)

    @staticmethod
    def _ordered_stops(config: dict[str, Any]) -> list[dict[str, Any]]:
        return sorted(config["route"]["stops"], key=lambda stop: stop["sequence"])

    @staticmethod
    def _next_stop(stops: list[dict[str, Any]], current_id: str, direction: int, loop: bool) -> tuple[dict[str, Any] | None, int]:
        if len(stops) < 2:
            return None, direction
        index_by_id = {stop["id"]: i for i, stop in enumerate(stops)}
        if current_id not in index_by_id:
            return None, direction
        index = index_by_id[current_id]
        candidate = index + (1 if direction >= 0 else -1)
        if 0 <= candidate < len(stops):
            return stops[candidate], 1 if direction >= 0 else -1
        if loop:
            return (stops[0] if direction >= 0 else stops[-1]), (1 if direction >= 0 else -1)
        reversed_direction = -1 if direction >= 0 else 1
        return stops[index + reversed_direction], reversed_direction

    @staticmethod
    def _leg_seconds(a: dict[str, Any], b: dict[str, Any], speed_kmph: float) -> float:
        return haversine_km(a, b) / max(speed_kmph, 0.1) * 3600.0

    def live_buses(self) -> list[dict[str, Any]]:
        config = self.config_store.get()
        stops = self._ordered_stops(config)
        stops_by_id = {stop["id"]: stop for stop in stops}
        bus_config = {bus["id"]: bus for bus in config["route"]["buses"]}
        speed = float(config["eta"]["assumedSpeedKmph"])
        stale_after_ms = int(config["dashboard"]["staleAfterSeconds"]) * 1000
        current_time = now_ms()

        with self.lock:
            bus_ids = list(dict.fromkeys([*bus_config.keys(), *self.buses.keys()]))
            result = []
            for bus_id in bus_ids:
                state = self.buses.get(bus_id)
                display = bus_config.get(bus_id, {"name": bus_id, "color": "#38bdf8"})
                if state is None:
                    result.append({
                        "busId": bus_id, "name": display.get("name", bus_id), "color": display.get("color", "#38bdf8"),
                        "lastStopId": None, "prevStopId": None, "direction": 1, "nextStopId": None,
                        "lastSeenMs": None, "lastRssi": None, "lastSeq": None,
                        "etaSecondsToNextStop": None, "legDurationSeconds": None, "stale": True,
                    })
                    continue
                current_stop = stops_by_id.get(state["lastStopId"])
                next_stop, travel_direction = self._next_stop(stops, state["lastStopId"], state["direction"], config["route"].get("loop", False))
                leg_seconds = self._leg_seconds(current_stop, next_stop, speed) if current_stop and next_stop else None
                elapsed = max(0.0, (current_time - state["lastSeenMs"]) / 1000.0)
                eta = max(0.0, leg_seconds - elapsed) if leg_seconds is not None else None
                result.append({
                    "busId": bus_id, "name": display.get("name", bus_id), "color": display.get("color", "#38bdf8"),
                    **state,
                    "direction": travel_direction,
                    "nextStopId": next_stop["id"] if next_stop else None,
                    "etaSecondsToNextStop": round(eta, 1) if eta is not None else None,
                    "legDurationSeconds": round(leg_seconds, 1) if leg_seconds is not None else None,
                    "stale": current_time - state["lastSeenMs"] > stale_after_ms,
                })
            return result

    def live_stops(self) -> list[dict[str, Any]]:
        config = self.config_store.get()
        stale_after_ms = int(config["dashboard"]["staleAfterSeconds"]) * 1000
        current_time = now_ms()
        with self.lock:
            result = []
            for configured in self._ordered_stops(config):
                health = self.stops.get(configured["id"])
                online = bool(health and current_time - health["lastHeartbeatMs"] <= stale_after_ms)
                result.append({
                    "stopId": configured["id"], "name": configured["name"], "lat": configured["lat"],
                    "lon": configured["lon"], "sequence": configured["sequence"], "online": online,
                    "linkMode": health.get("linkMode") if health else None,
                    "lastHeartbeatMs": health.get("lastHeartbeatMs") if health else None,
                    "uptimeSec": health.get("uptimeSec") if health else None,
                    "rssiFloorNoise": health.get("rssiFloorNoise") if health else None,
                })
            return result

    def arrivals(self, stop_id: str) -> list[dict[str, Any]] | None:
        config = self.config_store.get()
        stops = self._ordered_stops(config)
        stops_by_id = {stop["id"]: stop for stop in stops}
        if stop_id not in stops_by_id:
            return None
        speed = float(config["eta"]["assumedSpeedKmph"])
        loop = config["route"].get("loop", False)
        current_time = now_ms()
        live_by_id = {bus["busId"]: bus for bus in self.live_buses()}
        arrivals: list[dict[str, Any]] = []

        for bus in config["route"]["buses"]:
            live = live_by_id.get(bus["id"])
            if not live or not live.get("lastStopId"):
                continue
            if live["lastStopId"] == stop_id:
                eta = 0.0
            else:
                current_id = live["lastStopId"]
                direction = int(live.get("direction", 1))
                eta = 0.0
                found = False
                for leg_index in range(max(2, len(stops) * 3)):
                    current_stop = stops_by_id.get(current_id)
                    next_stop, direction = self._next_stop(stops, current_id, direction, loop)
                    if not current_stop or not next_stop:
                        break
                    leg = self._leg_seconds(current_stop, next_stop, speed)
                    if leg_index == 0:
                        elapsed = max(0.0, (current_time - live["lastSeenMs"]) / 1000.0)
                        leg = max(0.0, leg - elapsed)
                    eta += leg
                    current_id = next_stop["id"]
                    if current_id == stop_id:
                        found = True
                        break
                if not found:
                    continue
            arrivals.append({
                "busId": bus["id"], "name": bus["name"], "color": bus.get("color", "#38bdf8"),
                "etaSeconds": round(eta, 1), "stale": bool(live.get("stale")),
                "lastStopId": live.get("lastStopId"),
            })
        arrivals.sort(key=lambda item: item["etaSeconds"])
        return arrivals

    def recent_events(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.events)

    def status(self) -> dict[str, Any]:
        config = self.config_store.get()
        threshold_ms = int(config["dashboard"]["staleAfterSeconds"]) * 1000
        with self.lock:
            age = None if self.last_ingest_ms is None else max(0, now_ms() - self.last_ingest_ms)
            return {
                "online": age is not None and age <= threshold_ms,
                "lastMessageAtMs": self.last_ingest_ms,
                "lastMessageAgeMs": age,
                "transports": dict(self.transports),
            }
