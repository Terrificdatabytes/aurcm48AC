from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT / "backend"
os.environ["AEROTRACK_DISABLE_INGESTION"] = "1"
sys.path.insert(0, str(BACKEND))

from app import create_app  # noqa: E402


@pytest.fixture()
def config_value() -> dict:
    return json.loads((PROJECT / "config.json").read_text(encoding="utf-8"))


@pytest.fixture()
def app(tmp_path: Path, config_value: dict):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_value), encoding="utf-8")
    application = create_app(start_background=False, config_path=path)
    application.config.update(TESTING=True, SECRET_KEY="test-secret")
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, username="admin", password="changeme"):
    return client.post("/api/admin/login", json={"username": username, "password": password})


def sighting(stop_id: str, seq: int, bus_id: str = "BUS-01") -> dict:
    return {
        "schemaVersion": "1.0",
        "stopId": stop_id,
        "busId": bus_id,
        "rssi": -61,
        "seq": seq,
        "gatewayReceivedAtMs": int(time.time() * 1000),
    }


def test_real_pages_and_public_config_redaction(client):
    for path in ("/", "/stops", "/stops/STOP-A", "/buses", "/admin"):
        assert client.get(path).status_code == 200
    config = client.get("/api/config").get_json()
    assert config["route"]["name"].startswith("Campus Loop")
    assert config["admin"]["username"] == "admin"
    assert config["admin"]["password"] is None


def test_config_write_requires_login_and_is_shallow_section_replacement(client, config_value):
    response = client.post("/api/config", json={"eta": {"method": "interpolation", "assumedSpeedKmph": 25}})
    assert response.status_code == 401
    assert login(client).status_code == 200
    response = client.post("/api/config", json={"eta": {"method": "interpolation", "assumedSpeedKmph": 25}})
    assert response.status_code == 200
    updated = response.get_json()
    assert updated["eta"]["assumedSpeedKmph"] == 25
    assert updated["route"] == config_value["route"]


def test_invalid_config_does_not_overwrite_file(client, app):
    assert login(client).status_code == 200
    store = app.extensions["aerotrack_store"]
    original = store.path.read_text(encoding="utf-8")
    bad_route = copy.deepcopy(store.get()["route"])
    del bad_route["stops"][0]["lat"]
    response = client.post("/api/config", json={"route": bad_route})
    assert response.status_code == 400
    assert "lat" in response.get_json()["message"]
    assert store.path.read_text(encoding="utf-8") == original


def test_sighting_dedup_and_transition_direction(client, app):
    tracker = app.extensions["aerotrack_tracker"]
    assert tracker.process_sighting(sighting("STOP-A", 10), "test")
    assert not tracker.process_sighting(sighting("STOP-B", 10), "test")
    live = client.get("/api/buses/live").get_json()[0]
    assert live["lastStopId"] == "STOP-A"
    assert live["lastSeq"] == 10
    assert tracker.process_sighting(sighting("STOP-B", 11), "test")
    live = client.get("/api/buses/live").get_json()[0]
    assert live["prevStopId"] == "STOP-A"
    assert live["lastStopId"] == "STOP-B"
    # At an out-and-back terminus, the effective next direction is the return leg.
    assert live["direction"] == -1
    assert live["nextStopId"] == "STOP-A"
    assert live["etaSecondsToNextStop"] >= 0
    events = client.get("/api/events").get_json()
    assert any(event["kind"] == "dropped" for event in events)


def test_health_and_arrivals(client, app):
    tracker = app.extensions["aerotrack_tracker"]
    tracker.process_sighting(sighting("STOP-A", 1), "test")
    tracker.process_health({
        "schemaVersion": "1.0", "stopId": "STOP-A", "uptimeSec": 90,
        "linkMode": "uart_direct", "timestampMs": int(time.time() * 1000),
    }, "test")
    stops = client.get("/api/stops").get_json()
    assert stops[0]["online"] is True
    assert stops[0]["linkMode"] == "uart_direct"
    arrivals = client.get("/api/stops/STOP-B/arrivals").get_json()["arrivals"]
    assert arrivals[0]["busId"] == "BUS-01"
    assert arrivals[0]["etaSeconds"] >= 0


def test_empty_stop_config_returns_clean_empty_array(client, app):
    assert login(client).status_code == 200
    route = app.extensions["aerotrack_store"].get()["route"]
    route["stops"] = []
    assert client.post("/api/config", json={"route": route}).status_code == 200
    assert client.get("/api/stops").get_json() == []
    assert client.get("/api/buses/live").status_code == 200


def test_changed_password_replaces_old_credentials(client):
    assert login(client).status_code == 200
    response = client.post("/api/config", json={"admin": {"username": "operator", "password": "new-secret"}})
    assert response.status_code == 200
    client.post("/api/admin/logout", json={})
    assert login(client).status_code == 401
    assert login(client, "operator", "new-secret").status_code == 200


def test_database_storage_requires_admin_and_persists_settings(client, app):
    assert client.get("/api/admin/database").status_code == 401
    assert client.post("/api/admin/database/activate").status_code == 401
    assert login(client).status_code == 200

    initial = client.get("/api/admin/database").get_json()
    assert initial["mode"] == "json"
    assert initial["databaseExists"] is False

    response = client.post("/api/admin/database/activate")
    assert response.status_code == 200
    snapshot = response.get_json()
    assert snapshot["mode"] == "sqlite"
    assert snapshot["databaseExists"] is True
    assert {table["name"] for table in snapshot["tables"]} == {
        "configuration", "configuration_history", "metadata"
    }
    # Database views must not leak the plaintext prototype password.
    assert "changeme" not in response.get_data(as_text=True)

    json_before = json.loads(app.extensions["aerotrack_store"].path.read_text(encoding="utf-8"))
    update = client.post("/api/config", json={"eta": {"method": "interpolation", "assumedSpeedKmph": 31}})
    assert update.status_code == 200
    assert update.get_json()["eta"]["assumedSpeedKmph"] == 31
    # SQLite is now authoritative; the fallback JSON file is intentionally untouched.
    json_after = json.loads(app.extensions["aerotrack_store"].path.read_text(encoding="utf-8"))
    assert json_after == json_before

    status = client.get("/api/admin/database").get_json()
    assert status["mode"] == "sqlite"
    assert status["history"][0]["sections"] == ["eta"]
    assert status["configuration"][5]["value"]["assumedSpeedKmph"] == 31

    store = app.extensions["aerotrack_store"]
    restarted = create_app(
        start_background=False,
        config_path=store.path,
        database_path=store.database_path,
    )
    assert restarted.extensions["aerotrack_store"].get()["eta"]["assumedSpeedKmph"] == 31


def test_database_reset_switch_and_delete(client, app):
    assert login(client).status_code == 200
    store = app.extensions["aerotrack_store"]
    assert client.post("/api/admin/database/activate").status_code == 200
    assert client.post("/api/config", json={"eta": {"method": "interpolation", "assumedSpeedKmph": 37}}).status_code == 200

    assert client.post("/api/admin/database/reset", json={"confirmation": "wrong"}).status_code == 400
    reset = client.post("/api/admin/database/reset", json={"confirmation": "RESET"})
    assert reset.status_code == 200
    assert client.get("/api/config").get_json()["eta"]["assumedSpeedKmph"] == 20
    assert reset.get_json()["history"][0]["sections"] == ["database_reset"]

    assert client.post("/api/config", json={"eta": {"method": "interpolation", "assumedSpeedKmph": 29}}).status_code == 200
    switched = client.post("/api/admin/database/use-json")
    assert switched.status_code == 200
    assert switched.get_json()["mode"] == "json"
    assert json.loads(store.path.read_text(encoding="utf-8"))["eta"]["assumedSpeedKmph"] == 29

    # Re-enabling copies the current JSON settings into SQLite.
    assert client.post("/api/config", json={"eta": {"method": "interpolation", "assumedSpeedKmph": 33}}).status_code == 200
    activated = client.post("/api/admin/database/activate")
    assert activated.status_code == 200
    assert client.get("/api/config").get_json()["eta"]["assumedSpeedKmph"] == 33

    assert client.delete("/api/admin/database", json={"confirmation": "wrong"}).status_code == 400
    deleted = client.delete("/api/admin/database", json={"confirmation": "DELETE"})
    assert deleted.status_code == 200
    assert deleted.get_json()["mode"] == "json"
    assert deleted.get_json()["databaseExists"] is False
    assert not store.database_path.exists()
    assert client.get("/api/config").get_json()["eta"]["assumedSpeedKmph"] == 33
