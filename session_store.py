import copy
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    """Return the current UTC time formatted as ISO 8601."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _default_metrics() -> Dict[str, Any]:
    """Return default metrics structure for a session."""
    return {
        "token_usage": {
            "total_tokens": 0,
            "five_hour": {
                "used": 0,
                "limit": None,
                "remaining": None,
                "last_updated": None,
            },
            "weekly": {
                "used": 0,
                "limit": None,
                "remaining": None,
                "last_updated": None,
            },
            "warnings": {
                "five_hour": [],
                "weekly": [],
            },
        },
        "last_task_tokens": 0,
        "rate_limits": {},
    }


def _default_settings() -> Dict[str, Any]:
    """Return default runtime settings for a session."""
    return {
        "approval_policy": "never",
        "model": "default",
    }


def _default_metadata() -> Dict[str, Any]:
    """Return an empty metadata payload for a session."""
    return {
        "tasks": [],
        "metrics": _default_metrics(),
        "settings": _default_settings(),
    }


def _ensure_metadata_defaults(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure metadata payload contains default sections."""
    metadata.setdefault("tasks", [])

    metrics = metadata.get("metrics")
    if not isinstance(metrics, dict):
        metrics = copy.deepcopy(_default_metrics())
        metadata["metrics"] = metrics
    else:
        default_metrics = _default_metrics()
        for key, value in default_metrics.items():
            if key not in metrics or not isinstance(metrics[key], type(value)):
                metrics[key] = copy.deepcopy(value)

        # ensure nested warnings structure
        token_usage = metrics.setdefault("token_usage", copy.deepcopy(default_metrics["token_usage"]))
        warnings = token_usage.setdefault("warnings", {"five_hour": [], "weekly": []})
        if not isinstance(warnings, dict):
            warnings = {"five_hour": [], "weekly": []}
            token_usage["warnings"] = warnings
        warnings.setdefault("five_hour", [])
        warnings.setdefault("weekly", [])

    settings = metadata.get("settings")
    if not isinstance(settings, dict):
        settings = copy.deepcopy(_default_settings())
        metadata["settings"] = settings
    else:
        default_settings = _default_settings()
        for key, value in default_settings.items():
            settings.setdefault(key, value)

    return metadata


@dataclass
class SessionRecord:
    session_id: str
    branch_name: Optional[str]
    created_at: str
    updated_at: str
    metadata: Dict[str, Any]


class SessionStore:
    """Persistent store tracking Codex sessions and their metadata."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = db_path or os.path.join(base_dir, "codex_sessions.sqlite3")
        self._ensure_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    branch_name TEXT,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS livekit_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def ensure_session(self, session_id: str, branch_name: Optional[str]) -> SessionRecord:
        """Create the session record if it does not already exist."""
        now = _now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT session_id, branch_name, metadata, created_at, updated_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing:
                branch_to_store = branch_name or existing["branch_name"]
                conn.execute(
                    "UPDATE sessions SET branch_name = ?, updated_at = ? WHERE session_id = ?",
                    (branch_to_store, now, session_id),
                )
                metadata = _ensure_metadata_defaults(json.loads(existing["metadata"]))
                return SessionRecord(
                    session_id=session_id,
                    branch_name=branch_to_store,
                    created_at=existing["created_at"],
                    updated_at=now,
                    metadata=metadata,
                )

            metadata = _default_metadata()
            conn.execute(
                """
                INSERT INTO sessions (session_id, branch_name, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, branch_name, json.dumps(metadata), now, now),
            )
            return SessionRecord(
                session_id=session_id,
                branch_name=branch_name,
                created_at=now,
                updated_at=now,
                metadata=metadata,
            )

    def session_exists(self, session_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row is not None

    def list_sessions(self) -> List[SessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, branch_name, metadata, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
            return [
                SessionRecord(
                    session_id=row["session_id"],
                    branch_name=row["branch_name"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    metadata=_ensure_metadata_defaults(json.loads(row["metadata"])),
                )
                for row in rows
            ]

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT session_id, branch_name, metadata, created_at, updated_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            metadata = _ensure_metadata_defaults(json.loads(row["metadata"]))
            return SessionRecord(
                session_id=row["session_id"],
                branch_name=row["branch_name"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                metadata=metadata,
            )

    def update_branch(self, session_id: str, branch_name: str) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET branch_name = ?, updated_at = ? WHERE session_id = ?",
                (branch_name, now, session_id),
            )

    def append_task(self, session_id: str, prompt: str, result: Optional[str]) -> None:
        self.append_entry(session_id, prompt=prompt, result=result, entry_type="task")

    def append_entry(
        self,
        session_id: str,
        *,
        prompt: str,
        result: Optional[str],
        entry_type: str = "task",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Generalized append with custom entry type."""
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT branch_name, metadata, created_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                metadata = _default_metadata()
                created_at = now
                branch_name = None
            else:
                metadata = _ensure_metadata_defaults(json.loads(row["metadata"]))
                created_at = row["created_at"]
                branch_name = row["branch_name"]

            entry: Dict[str, Any] = {
                "prompt": prompt,
                "result": result,
                "timestamp": now,
                "type": entry_type,
            }
            if extra:
                entry["extra"] = extra

            metadata["tasks"].append(entry)

            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, branch_name, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, branch_name, json.dumps(metadata), created_at, now),
            )
            conn.execute(
                "UPDATE sessions SET metadata = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(metadata), now, session_id),
            )

    def update_metrics(self, session_id: str, metrics_update: Dict[str, Any]) -> None:
        """Merge metrics payload into stored metadata."""
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT branch_name, metadata, created_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                metadata = _default_metadata()
                created_at = now
                branch_name = None
            else:
                metadata = _ensure_metadata_defaults(json.loads(row["metadata"]))
                created_at = row["created_at"]
                branch_name = row["branch_name"]

            stored_metrics: Dict[str, Any] = metadata.setdefault("metrics", _default_metrics())
            _deep_update(stored_metrics, metrics_update)

            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, branch_name, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, branch_name, json.dumps(metadata), created_at, now),
            )
            conn.execute(
                "UPDATE sessions SET metadata = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(metadata), now, session_id),
            )

    def record_usage_warning(self, session_id: str, window: str, threshold: int) -> None:
        """Persist a usage warning level that has been communicated to the user."""
        if window not in {"five_hour", "weekly"}:
            return
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT branch_name, metadata, created_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                metadata = _default_metadata()
                created_at = now
                branch_name = None
            else:
                metadata = _ensure_metadata_defaults(json.loads(row["metadata"]))
                created_at = row["created_at"]
                branch_name = row["branch_name"]

            metrics = metadata.setdefault("metrics", _default_metrics())
            token_usage = metrics.setdefault("token_usage", _default_metrics()["token_usage"])
            warnings = token_usage.setdefault("warnings", {"five_hour": [], "weekly": []})
            levels: List[int] = warnings.setdefault(window, [])
            if threshold not in levels:
                levels.append(threshold)
                levels.sort()

            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, branch_name, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, branch_name, json.dumps(metadata), created_at, now),
            )
            conn.execute(
                "UPDATE sessions SET metadata = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(metadata), now, session_id),
            )

    def get_metrics(self, session_id: str) -> Optional[Dict[str, Any]]:
        record = self.get_session(session_id)
        if not record:
            return None
        return record.metadata.get("metrics", _default_metrics())

    def update_settings(self, session_id: str, settings_update: Dict[str, Any]) -> None:
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT branch_name, metadata, created_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                metadata = _default_metadata()
                created_at = now
                branch_name = None
            else:
                metadata = _ensure_metadata_defaults(json.loads(row["metadata"]))
                created_at = row["created_at"]
                branch_name = row["branch_name"]

            settings = metadata.setdefault("settings", _default_settings())
            settings.update(settings_update)

            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, branch_name, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, branch_name, json.dumps(metadata), created_at, now),
            )
            conn.execute(
                "UPDATE sessions SET metadata = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(metadata), now, session_id),
            )

    def get_settings(self, session_id: str) -> Optional[Dict[str, Any]]:
        record = self.get_session(session_id)
        if not record:
            return None
        settings = record.metadata.get("settings")
        if isinstance(settings, dict):
            return settings
        return _default_settings()

    def rename_session(self, old_session_id: str, new_session_id: str) -> bool:
        """Rename a session if the new id is not already used."""
        if old_session_id == new_session_id:
            return True
        now = _now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?",
                (new_session_id,),
            ).fetchone()
            if existing:
                return False
            updated = conn.execute(
                "UPDATE sessions SET session_id = ?, updated_at = ? WHERE session_id = ?",
                (new_session_id, now, old_session_id),
            )
            return updated.rowcount > 0

    def get_livekit_state(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM livekit_state WHERE key = ?",
                ("voice_assistant",),
            ).fetchone()
            if not row:
                return {}
            try:
                state = json.loads(row["value"])
                if isinstance(state, dict):
                    return state
            except json.JSONDecodeError:
                return {}
            return {}

    def set_livekit_state(self, state: Dict[str, Any]) -> None:
        now = _now_iso()
        payload = json.dumps(state)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO livekit_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                ("voice_assistant", payload, now),
            )


def _deep_update(target: Dict[str, Any], updates: Dict[str, Any]) -> None:
    """Recursively merge update dict into target dict."""
    for key, value in updates.items():
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(value, dict)
        ):
            _deep_update(target[key], value)
        else:
            target[key] = value
