"""AeroTrack Transit: Flask dashboard, REST API and ingestion process."""
from __future__ import annotations

import atexit
import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from config_store import ConfigError, ConfigStore
from tracker import TransitTracker

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


def create_app(
    *,
    start_background: bool = True,
    config_path: str | Path | None = None,
    database_path: str | Path | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config.update(JSON_SORT_KEYS=False, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    configured_secret = os.environ.get("AEROTRACK_SECRET_KEY")
    if configured_secret:
        app.secret_key = configured_secret
    else:
        app.secret_key = secrets.token_bytes(32)
        app.logger.warning("AEROTRACK_SECRET_KEY is unset; sessions will reset whenever Flask restarts.")

    actual_config_path = Path(config_path or os.environ.get("AEROTRACK_CONFIG", PROJECT_DIR / "config.json"))
    actual_database_path = Path(
        database_path or os.environ.get("AEROTRACK_DATABASE", actual_config_path.with_name("aerotrack.db"))
    )
    store = ConfigStore(
        actual_config_path,
        PROJECT_DIR / "schemas" / "config.schema.json",
        actual_database_path,
    )
    tracker = TransitTracker(store, PROJECT_DIR / "schemas")
    app.extensions["aerotrack_store"] = store
    app.extensions["aerotrack_tracker"] = tracker

    ingestion = None
    if start_background:
        from ingestion import IngestionSupervisor
        ingestion = IngestionSupervisor(store, tracker)
        ingestion.start()
        app.extensions["aerotrack_ingestion"] = ingestion
        atexit.register(ingestion.stop)

    @app.after_request
    def disable_api_cache(response: Any) -> Any:
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def shared_template_data() -> dict[str, Any]:
        try:
            public = store.get_public()
            return {"route_name": public["route"]["name"]}
        except ConfigError:
            return {"route_name": "AeroTrack Transit"}

    # Real, independently loadable pages (no client-side router).
    @app.get("/")
    def live_map() -> str:
        return render_template("index.html", active_page="map", page_title="Live map")

    @app.get("/stops")
    def stops_page() -> str:
        return render_template("stops.html", active_page="stops", page_title="Stops")

    @app.get("/stops/<stop_id>")
    def stop_detail_page(stop_id: str) -> tuple[str, int] | str:
        stop = next((item for item in tracker.live_stops() if item["stopId"] == stop_id), None)
        if stop is None:
            return render_template("not_found.html", active_page="stops", page_title="Stop not found", item=stop_id), 404
        return render_template("stop_detail.html", active_page="stops", page_title=stop["name"], stop=stop)

    @app.get("/buses")
    def buses_page() -> str:
        return render_template("buses.html", active_page="buses", page_title="Buses")

    @app.get("/admin")
    def admin_page() -> str:
        if not session.get("admin_authenticated"):
            return render_template("admin_login.html", active_page="admin", page_title="Admin sign in")
        return render_template("admin.html", active_page="admin", page_title="Configuration")

    # REST API.
    @app.get("/api/config")
    def get_config() -> Any:
        return jsonify(store.get_public())

    @app.post("/api/config")
    def update_config() -> tuple[Any, int] | Any:
        if not session.get("admin_authenticated"):
            return jsonify({"error": "authentication_required", "message": "Sign in as an administrator before saving configuration."}), 401
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "invalid_json", "message": "Request body must be a JSON object."}), 400
        try:
            store.patch(body)
        except ConfigError as exc:
            return jsonify(exc.as_dict()), 400
        with tracker.lock:
            tracker._event("config", f"Configuration updated: {', '.join(body.keys())}", sections=list(body.keys()))
        return jsonify(store.get_public())

    @app.get("/api/buses/live")
    def get_live_buses() -> Any:
        return jsonify(tracker.live_buses())

    @app.get("/api/stops")
    def get_stops() -> Any:
        return jsonify(tracker.live_stops())

    @app.get("/api/stops/<stop_id>/arrivals")
    def get_arrivals(stop_id: str) -> tuple[Any, int] | Any:
        arrivals = tracker.arrivals(stop_id)
        if arrivals is None:
            return jsonify({"error": "not_found", "message": f"Unknown stop: {stop_id}"}), 404
        stop = next(item for item in tracker.live_stops() if item["stopId"] == stop_id)
        return jsonify({"stop": stop, "arrivals": arrivals, "generatedAtMs": __import__("time").time_ns() // 1_000_000})

    @app.get("/api/events")
    def get_events() -> Any:
        return jsonify(tracker.recent_events())

    @app.get("/api/status")
    def get_status() -> Any:
        return jsonify(tracker.status())

    @app.post("/api/admin/login")
    def admin_login() -> tuple[Any, int] | Any:
        body = request.get_json(silent=True) or request.form
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        credentials = store.get()["admin"]
        if hmac.compare_digest(username, credentials["username"]) and hmac.compare_digest(password, credentials["password"]):
            session.clear()
            session["admin_authenticated"] = True
            session["admin_username"] = username
            return jsonify({"ok": True, "redirect": url_for("admin_page")})
        return jsonify({"error": "invalid_credentials", "message": "The username or password is incorrect."}), 401

    @app.post("/api/admin/logout")
    def admin_logout() -> Any:
        session.clear()
        if request.is_json:
            return jsonify({"ok": True})
        return redirect(url_for("admin_page"))

    @app.get("/api/admin/session")
    def admin_session() -> Any:
        return jsonify({"authenticated": bool(session.get("admin_authenticated")), "username": session.get("admin_username")})

    def database_admin_required() -> tuple[Any, int] | None:
        if not session.get("admin_authenticated"):
            return jsonify({
                "error": "authentication_required",
                "message": "Administrator authentication is required for database operations.",
            }), 401
        return None

    @app.get("/api/admin/database")
    def admin_database_view() -> tuple[Any, int] | Any:
        denied = database_admin_required()
        if denied:
            return denied
        return jsonify(store.database_snapshot())

    @app.post("/api/admin/database/activate")
    def admin_database_activate() -> tuple[Any, int] | Any:
        denied = database_admin_required()
        if denied:
            return denied
        store.activate_database()
        with tracker.lock:
            tracker._event("database", "SQLite configuration storage enabled", action="activate")
        return jsonify(store.database_snapshot())

    @app.post("/api/admin/database/use-json")
    def admin_database_use_json() -> tuple[Any, int] | Any:
        denied = database_admin_required()
        if denied:
            return denied
        store.use_json()
        with tracker.lock:
            tracker._event("database", "JSON configuration storage enabled", action="use_json")
        return jsonify(store.database_snapshot())

    @app.post("/api/admin/database/reset")
    def admin_database_reset() -> tuple[Any, int] | Any:
        denied = database_admin_required()
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        if body.get("confirmation") != "RESET":
            return jsonify({
                "error": "confirmation_required",
                "message": "Type RESET to restore the database's initial settings and clear its history.",
            }), 400
        store.reset_database()
        with tracker.lock:
            tracker._event("database", "SQLite database reset to its initial snapshot", action="reset")
        return jsonify(store.database_snapshot())

    @app.delete("/api/admin/database")
    def admin_database_delete() -> tuple[Any, int] | Any:
        denied = database_admin_required()
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        if body.get("confirmation") != "DELETE":
            return jsonify({
                "error": "confirmation_required",
                "message": "Type DELETE to remove the SQLite database file.",
            }), 400
        store.delete_database()
        with tracker.lock:
            tracker._event("database", "SQLite database deleted; JSON storage is active", action="delete")
        return jsonify(store.database_snapshot())

    @app.errorhandler(ConfigError)
    def handle_config_error(exc: ConfigError) -> tuple[Any, int] | tuple[str, int]:
        if request.path.startswith("/api/"):
            return jsonify(exc.as_dict()), 500
        return render_template("error.html", active_page="", page_title="Configuration error", message=exc.message), 500

    return app


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
app = create_app(start_background=os.environ.get("AEROTRACK_DISABLE_INGESTION") != "1")

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG") == "1", use_reloader=False)
