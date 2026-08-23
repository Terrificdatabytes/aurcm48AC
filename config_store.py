"""Thread-safe, schema-validated JSON or SQLite configuration storage."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class ConfigError(ValueError):
    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.path = path

    def as_dict(self) -> dict[str, str]:
        return {"error": "validation_failed", "message": self.message, "path": self.path}


class ConfigStore:
    """Validated configuration with a live-selectable JSON/SQLite backend.

    JSON remains the safe fallback. When SQLite is enabled, an ``active`` marker
    in the database makes it the source of truth across Flask and simulator
    restarts. All public snapshots redact the prototype admin password.
    """

    SECTIONS = {"route", "network", "serialFallback", "espnow", "rs485", "eta", "dashboard", "admin"}
    HISTORY_LIMIT = 200

    def __init__(self, config_path: Path, schema_path: Path, database_path: Path | None = None) -> None:
        self.path = Path(config_path)
        self.schema_path = Path(schema_path)
        self.database_path = Path(database_path) if database_path else self.path.with_name("aerotrack.db")
        self._lock = threading.RLock()
        with self.schema_path.open("r", encoding="utf-8") as handle:
            self._schema = json.load(handle)
        self._validator = Draft202012Validator(self._schema)
        # Validate the source that will actually be used at startup.
        with self._lock:
            self._validate(self._read_active_unlocked())

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.database_path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.Error as exc:
            raise ConfigError(f"Could not open SQLite database {self.database_path}: {exc}") from exc

    @staticmethod
    def _create_database_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS configuration (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                document TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS configuration_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                updated_at TEXT NOT NULL,
                sections TEXT NOT NULL,
                document TEXT NOT NULL
            );
            """
        )

    def _database_active_unlocked(self) -> bool:
        if not self.database_path.is_file():
            return False
        try:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT value FROM metadata WHERE key = 'active'").fetchone()
                return bool(row and row["value"] == "1")
        except sqlite3.OperationalError as exc:
            # An empty/new database has no metadata table and is simply inactive.
            if "no such table" in str(exc).lower():
                return False
            raise ConfigError(f"Could not inspect SQLite database {self.database_path}: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise ConfigError(f"Invalid SQLite database {self.database_path}: {exc}") from exc

    def _read_json_unlocked(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Could not read valid JSON from {self.path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ConfigError("Configuration root must be a JSON object")
        return value

    def _read_database_unlocked(self) -> dict[str, Any]:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT document FROM configuration WHERE id = 1").fetchone()
        except sqlite3.Error as exc:
            raise ConfigError(f"Could not read configuration from SQLite: {exc}") from exc
        if row is None:
            raise ConfigError("SQLite is active but contains no configuration row")
        try:
            value = json.loads(row["document"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"SQLite configuration contains invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ConfigError("SQLite configuration root must be a JSON object")
        return value

    def _read_active_unlocked(self) -> dict[str, Any]:
        if self._database_active_unlocked():
            return self._read_database_unlocked()
        return self._read_json_unlocked()

    @staticmethod
    def _error_path(error: Any) -> str:
        return ".".join(str(part) for part in error.absolute_path)

    def _validate(self, value: dict[str, Any]) -> None:
        errors = sorted(self._validator.iter_errors(value), key=lambda e: (list(e.absolute_path), e.message))
        if errors:
            error = errors[0]
            path = self._error_path(error)
            label = path or "config"
            raise ConfigError(f"{label}: {error.message}", path)

        route = value["route"]
        for collection, label in ((route["stops"], "route.stops"), (route["buses"], "route.buses")):
            ids = [item["id"] for item in collection]
            if len(ids) != len(set(ids)):
                raise ConfigError(f"{label}: ids must be unique", label)
        sequences = [item["sequence"] for item in route["stops"]]
        if len(sequences) != len(set(sequences)):
            raise ConfigError("route.stops: sequence values must be unique", "route.stops")

    def _write_json_unlocked(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _write_database_unlocked(self, value: dict[str, Any], sections: list[str]) -> None:
        now = self._now()
        document = self._json(value)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    self._create_database_schema(connection)
                    connection.execute(
                        "INSERT INTO configuration(id, document, updated_at) VALUES(1, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET document=excluded.document, updated_at=excluded.updated_at",
                        (document, now),
                    )
                    connection.execute(
                        "INSERT INTO configuration_history(updated_at, sections, document) VALUES(?, ?, ?)",
                        (now, self._json(sections), document),
                    )
                    connection.execute(
                        "DELETE FROM configuration_history WHERE id NOT IN "
                        "(SELECT id FROM configuration_history ORDER BY id DESC LIMIT ?)",
                        (self.HISTORY_LIMIT,),
                    )
        except sqlite3.Error as exc:
            raise ConfigError(f"Could not write configuration to SQLite: {exc}") from exc

    @staticmethod
    def _redact(value: dict[str, Any]) -> dict[str, Any]:
        public = copy.deepcopy(value)
        if isinstance(public.get("admin"), dict):
            public["admin"]["password"] = None
        return public

    def get(self) -> dict[str, Any]:
        with self._lock:
            value = self._read_active_unlocked()
            self._validate(value)
            return copy.deepcopy(value)

    def get_public(self) -> dict[str, Any]:
        return self._redact(self.get())

    def patch(self, sections: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(sections, dict) or not sections:
            raise ConfigError("Request body must contain at least one top-level config section")
        unknown = set(sections) - self.SECTIONS
        if unknown:
            name = sorted(unknown)[0]
            raise ConfigError(f"Unknown top-level section: {name}", name)

        with self._lock:
            database_active = self._database_active_unlocked()
            candidate = self._read_database_unlocked() if database_active else self._read_json_unlocked()
            # Deliberately shallow: each supplied top-level section is replaced wholesale.
            for key, value in sections.items():
                candidate[key] = copy.deepcopy(value)
            self._validate(candidate)
            if database_active:
                self._write_database_unlocked(candidate, sorted(sections))
            else:
                self._write_json_unlocked(candidate)
            return copy.deepcopy(candidate)

    def storage_status(self) -> dict[str, Any]:
        with self._lock:
            exists = self.database_path.is_file()
            active = self._database_active_unlocked() if exists else False
            return {
                "mode": "sqlite" if active else "json",
                "jsonPath": str(self.path),
                "databasePath": str(self.database_path),
                "databaseExists": exists,
                "databaseSizeBytes": self.database_path.stat().st_size if exists else 0,
            }

    def activate_database(self) -> None:
        """Copy the current JSON settings into SQLite and make SQLite active."""
        with self._lock:
            if self._database_active_unlocked():
                return
            current = self._read_json_unlocked()
            self._validate(current)
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            now = self._now()
            document = self._json(current)
            try:
                with closing(self._connect()) as connection:
                    with connection:
                        self._create_database_schema(connection)
                        baseline = connection.execute("SELECT value FROM metadata WHERE key='baseline_config'").fetchone()
                        if baseline is None:
                            connection.execute("INSERT INTO metadata(key, value) VALUES('baseline_config', ?)", (document,))
                            connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('created_at', ?)", (now,))
                        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('active', '1')")
                        connection.execute(
                            "INSERT INTO configuration(id, document, updated_at) VALUES(1, ?, ?) "
                            "ON CONFLICT(id) DO UPDATE SET document=excluded.document, updated_at=excluded.updated_at",
                            (document, now),
                        )
                        connection.execute(
                            "INSERT INTO configuration_history(updated_at, sections, document) VALUES(?, ?, ?)",
                            (now, self._json(["database_enabled"]), document),
                        )
            except sqlite3.Error as exc:
                raise ConfigError(f"Could not enable SQLite storage: {exc}") from exc

    def use_json(self) -> None:
        """Export active SQLite settings to config.json, then select JSON storage."""
        with self._lock:
            if not self._database_active_unlocked():
                return
            current = self._read_database_unlocked()
            self._validate(current)
            self._write_json_unlocked(current)
            try:
                with closing(self._connect()) as connection:
                    with connection:
                        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('active', '0')")
            except sqlite3.Error as exc:
                raise ConfigError(f"Could not switch to JSON storage: {exc}") from exc

    def reset_database(self) -> None:
        """Restore the first database snapshot and clear its modification history."""
        with self._lock:
            if not self.database_path.is_file():
                raise ConfigError("SQLite database does not exist")
            try:
                with closing(self._connect()) as connection:
                    baseline_row = connection.execute("SELECT value FROM metadata WHERE key='baseline_config'").fetchone()
                    active_row = connection.execute("SELECT value FROM metadata WHERE key='active'").fetchone()
                    if baseline_row is None:
                        raise ConfigError("SQLite database has no reset snapshot")
                    baseline = json.loads(baseline_row["value"])
                    self._validate(baseline)
                    now = self._now()
                    with connection:
                        connection.execute("DELETE FROM configuration_history")
                        connection.execute("DELETE FROM sqlite_sequence WHERE name='configuration_history'")
                        connection.execute(
                            "INSERT INTO configuration(id, document, updated_at) VALUES(1, ?, ?) "
                            "ON CONFLICT(id) DO UPDATE SET document=excluded.document, updated_at=excluded.updated_at",
                            (self._json(baseline), now),
                        )
                        connection.execute(
                            "INSERT INTO configuration_history(updated_at, sections, document) VALUES(?, ?, ?)",
                            (now, self._json(["database_reset"]), self._json(baseline)),
                        )
                        connection.execute(
                            "INSERT OR REPLACE INTO metadata(key, value) VALUES('active', ?)",
                            (active_row["value"] if active_row else "0",),
                        )
            except ConfigError:
                raise
            except (sqlite3.Error, json.JSONDecodeError) as exc:
                raise ConfigError(f"Could not reset SQLite database: {exc}") from exc

    def delete_database(self) -> None:
        """Delete SQLite files; active settings are exported to JSON first."""
        with self._lock:
            if not self.database_path.exists():
                return
            if self._database_active_unlocked():
                current = self._read_database_unlocked()
                self._validate(current)
                self._write_json_unlocked(current)
            for candidate in (
                self.database_path,
                Path(f"{self.database_path}-wal"),
                Path(f"{self.database_path}-shm"),
                Path(f"{self.database_path}-journal"),
            ):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError as exc:
                    raise ConfigError(f"Could not delete database file {candidate}: {exc}") from exc

    def database_snapshot(self) -> dict[str, Any]:
        """Return a password-redacted, admin-facing view of SQLite tables."""
        with self._lock:
            status = self.storage_status()
            if not status["databaseExists"]:
                return {**status, "tables": [], "configuration": [], "history": [], "metadata": []}
            try:
                with closing(self._connect()) as connection:
                    self._create_database_schema(connection)
                    config_row = connection.execute(
                        "SELECT document, updated_at FROM configuration WHERE id=1"
                    ).fetchone()
                    history_rows = connection.execute(
                        "SELECT id, updated_at, sections, document FROM configuration_history ORDER BY id DESC LIMIT 50"
                    ).fetchall()
                    metadata_rows = connection.execute("SELECT key, value FROM metadata ORDER BY key").fetchall()
                    counts = {
                        name: connection.execute(f"SELECT COUNT(*) AS count FROM {name}").fetchone()["count"]
                        for name in ("configuration", "configuration_history", "metadata")
                    }
            except sqlite3.Error as exc:
                raise ConfigError(f"Could not inspect SQLite database: {exc}") from exc

            configuration: list[dict[str, Any]] = []
            if config_row:
                document = self._redact(json.loads(config_row["document"]))
                configuration = [
                    {"section": key, "value": value, "updatedAt": config_row["updated_at"]}
                    for key, value in document.items()
                ]

            history = []
            for row in history_rows:
                history.append({
                    "id": row["id"],
                    "updatedAt": row["updated_at"],
                    "sections": json.loads(row["sections"]),
                    "configuration": self._redact(json.loads(row["document"])),
                })

            metadata = []
            for row in metadata_rows:
                if row["key"] == "baseline_config":
                    value: Any = self._redact(json.loads(row["value"]))
                else:
                    value = row["value"]
                metadata.append({"key": row["key"], "value": value})

            return {
                **status,
                "tables": [
                    {"name": "configuration", "rowCount": counts["configuration"]},
                    {"name": "configuration_history", "rowCount": counts["configuration_history"]},
                    {"name": "metadata", "rowCount": counts["metadata"]},
                ],
                "configuration": configuration,
                "history": history,
                "metadata": metadata,
            }
