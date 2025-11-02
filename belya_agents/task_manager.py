from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _timestamp() -> str:
    """Return a UTC timestamp string with second precision."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


@dataclass
class TaskRecord:
    """Canonical representation of a managed task."""

    id: str
    agent: str
    description: str
    status: str = "not_started"
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    metadata: Dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    error: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert the task record to a JSON serialisable structure."""
        payload = {
            "id": self.id,
            "agent": self.agent,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
            "result": self.result,
            "error": self.error,
            "history": self.history,
        }
        return payload


class TaskManager:
    """Persist and query agent tasks backed by a JSON file."""

    VALID_STATUSES = ("not_started", "in_progress", "completed", "failed")

    def __init__(self, tasks_file: Path | str = "tasks.json") -> None:
        self.tasks_file = Path(tasks_file)
        self._lock = threading.Lock()
        self._ensure_store_exists()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def add_task(
        self,
        *,
        agent: str,
        description: str,
        task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Add a task to the store and return the generated identifier."""
        if not agent:
            raise ValueError("agent must be provided")
        if not description:
            raise ValueError("description must be provided")

        record = TaskRecord(
            id=task_id or uuid.uuid4().hex,
            agent=agent,
            description=description,
            metadata=metadata or {},
            history=[self._history_entry("not_started", "Task created")],
        )

        with self._lock:
            tasks = self._load_raw_tasks()
            tasks.append(record.to_dict())
            self._write_raw_tasks(tasks)
        return record.id

    def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        note: Optional[str] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update the status and optional metadata for a task."""
        normalized_status = self._normalize_status(status)
        updated = False
        debug_note = note or ""
        with self._lock:
            tasks = self._load_raw_tasks()
            for entry in tasks:
                if entry.get("id") != task_id:
                    continue
                entry["status"] = normalized_status
                entry["updated_at"] = _timestamp()
                if result is not None:
                    entry["result"] = result
                if error is not None:
                    entry["error"] = error
                if metadata_update:
                    existing_meta = entry.setdefault("metadata", {})
                    if not isinstance(existing_meta, dict):
                        existing_meta = {}
                    existing_meta.update(metadata_update)
                    entry["metadata"] = existing_meta
                history_entry = self._history_entry(normalized_status, debug_note)
                entry.setdefault("history", []).append(history_entry)
                updated = True
                break
            if not updated:
                raise KeyError(f"Task {task_id} not found")
            self._write_raw_tasks(tasks)

    def append_task_note(self, task_id: str, note: str) -> None:
        """Attach a free form note to the task history without status change."""
        if not note:
            return
        with self._lock:
            tasks = self._load_raw_tasks()
            for entry in tasks:
                if entry.get("id") != task_id:
                    continue
                entry.setdefault("history", []).append(self._history_entry(entry.get("status", ""), note))
                entry["updated_at"] = _timestamp()
                self._write_raw_tasks(tasks)
                return
        raise KeyError(f"Task {task_id} not found")

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return the task data for the given identifier."""
        with self._lock:
            tasks = self._load_raw_tasks()
            for entry in tasks:
                if entry.get("id") == task_id:
                    return entry
        return None

    def get_tasks(
        self,
        *,
        status: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return tasks optionally filtered by status or owning agent."""
        desired_status = self._normalize_status(status) if status else None
        with self._lock:
            tasks = self._load_raw_tasks()
            filtered: List[Dict[str, Any]] = []
            for entry in tasks:
                if desired_status and entry.get("status") != desired_status:
                    continue
                if agent and entry.get("agent") != agent:
                    continue
                filtered.append(entry)
            return filtered

    def clear_completed_tasks(self) -> None:
        """Remove tasks that are completed to keep the backlog lean."""
        with self._lock:
            tasks = self._load_raw_tasks()
            active = [entry for entry in tasks if entry.get("status") != "completed"]
            self._write_raw_tasks(active)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _ensure_store_exists(self) -> None:
        if self.tasks_file.exists():
            return
        with self._lock:
            if self.tasks_file.exists():
                return
            self.tasks_file.write_text(json.dumps({"tasks": []}, indent=2))

    def _load_raw_tasks(self) -> List[Dict[str, Any]]:
        try:
            payload = json.loads(self.tasks_file.read_text())
            tasks = payload.get("tasks", [])
            if isinstance(tasks, list):
                return tasks
        except json.JSONDecodeError:
            pass
        return []

    def _write_raw_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        payload = {"tasks": tasks}
        self.tasks_file.write_text(json.dumps(payload, indent=2))

    def _normalize_status(self, status: str) -> str:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid task status '{status}'. Expected one of {self.VALID_STATUSES}.")
        return status

    @staticmethod
    def _history_entry(status: str, note: str) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"timestamp": _timestamp()}
        if status:
            entry["status"] = status
        if note:
            entry["note"] = note
        return entry


def demo_task_progression(tasks_path: Path | str = "tasks.json") -> None:
    """Demonstrate how a task moves from not started to completed."""
    manager = TaskManager(tasks_file=tasks_path)
    task_id = manager.add_task(agent="codex-belya", description="Demo long running task", metadata={"demo": True})
    manager.update_task_status(task_id, "in_progress", note="Background worker started")
    manager.update_task_status(task_id, "completed", note="Background worker finished", result="All done!")
    task = manager.get_task(task_id)
    print(json.dumps(task, indent=2))


if __name__ == "__main__":
    demo_task_progression("tasks_demo.json")
