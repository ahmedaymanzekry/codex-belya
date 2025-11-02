from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple, Type

from livekit.agents import Agent, RunContext, function_tool
from rich.console import Console
from rich.logging import RichHandler

from .codex_belya import CodexBelyaAgent
from .git_belya import GitBelyaAgent
from .rag_belya import RAGBelyaAgent
from .shared import AgentUtilitiesMixin
from .task_manager import TaskManager
from session_store import SessionRecord, SessionStore
from tools.codex_tools import CodexTaskResult
from tools.session_tools import SessionManagementToolsMixin

TaskRecord = Dict[str, Any]
SESSION_LOG_PATH: Path | None = None
_LOGGING_CONFIGURED = False
_CONSOLE = Console()


class _ConsoleRawLogFilter(logging.Filter):
    """Filter that suppresses console records coming from non-application loggers."""

    _ALLOWED_NAMES = {"basic-agent", "belya-agents"}
    _ALLOWED_PREFIXES = (
        "codex_belya",
        "head-belya",
        "tools",
        "mcp_server",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        name = getattr(record, "name", "") or ""
        if name in self._ALLOWED_NAMES:
            return True
        return any(name.startswith(prefix) for prefix in self._ALLOWED_PREFIXES)


def _configure_beautified_logging() -> logging.Logger:
    """Configure Rich-backed console logging and raw session file logging."""
    global SESSION_LOG_PATH, _LOGGING_CONFIGURED

    root_logger = logging.getLogger()
    if _LOGGING_CONFIGURED and SESSION_LOG_PATH and root_logger.handlers:
        return logging.getLogger("codex_belya")

    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    SESSION_LOG_PATH = logs_dir / f"session_{session_stamp}.log"

    console_handler = RichHandler(
        console=_CONSOLE,
        rich_tracebacks=True,
        show_time=True,
        show_path=False,
        log_time_format="%H:%M:%S",
    )
    console_handler.addFilter(_ConsoleRawLogFilter())
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    file_handler = logging.FileHandler(SESSION_LOG_PATH, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    _LOGGING_CONFIGURED = True

    app_logger = logging.getLogger("codex_belya")
    app_logger.setLevel(logging.DEBUG)
    app_logger.propagate = True
    app_logger.info("Codex Belya head agent logging initialized; beautified console active.")
    if SESSION_LOG_PATH:
        app_logger.info("Session log file capturing all records: %s", SESSION_LOG_PATH)

    return app_logger


APPLICATION_LOGGER = _configure_beautified_logging()
logger = logging.getLogger("head-belya")
logger.setLevel(logging.DEBUG)


class TaskRepository:
    """Caches and indexes Codex tasks stored in a ``tasks.json`` file."""

    def __init__(self, file_path: str) -> None:
        if not file_path:
            raise ValueError("TaskRepository requires a file path.")
        self.file_path = os.path.abspath(file_path)
        self._cache: Optional[List[TaskRecord]] = None
        self._last_mtime_ns: Optional[int] = None
        self._task_index: Dict[str, TaskRecord] = {}
        self._lock = threading.RLock()

    def load_tasks(self) -> Sequence[TaskRecord]:
        """Load tasks from disk, refreshing the cache only when needed."""
        with self._lock:
            try:
                stat_result = os.stat(self.file_path)
            except FileNotFoundError:
                self._cache = []
                self._task_index = {}
                self._last_mtime_ns = None
                return self._cache

            mtime_ns = getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1e9))
            if self._cache is not None and self._last_mtime_ns == mtime_ns:
                return self._cache

            with open(self.file_path, "r", encoding="utf-8") as stream:
                parsed = json.load(stream)

            raw_tasks: Iterable[Any]
            if isinstance(parsed, list):
                raw_tasks = parsed
            elif isinstance(parsed, dict):
                maybe_tasks = parsed.get("tasks", [])
                raw_tasks = maybe_tasks if isinstance(maybe_tasks, list) else []
            else:
                raw_tasks = []

            tasks: List[TaskRecord] = []
            for item in raw_tasks:
                if not isinstance(item, dict):
                    continue
                task_id = item.get("taskId") or item.get("id")
                if not task_id:
                    continue
                history = item.get("history")
                if not isinstance(history, list):
                    history = []
                tasks.append(
                    {
                        "taskId": str(task_id),
                        "history": history,
                        "raw": item,
                    }
                )

            self._cache = tasks
            self._last_mtime_ns = mtime_ns
            self._rebuild_index()

            return self._cache

    def refresh(self, force: bool = True) -> Sequence[TaskRecord]:
        """Invalidate the cache so the next load reflects on-disk changes."""
        with self._lock:
            if force:
                self._cache = None
                self._last_mtime_ns = None
        return self.load_tasks()

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """Return the cached task record, refreshing if necessary."""
        if not task_id:
            raise ValueError("task_id must be provided.")
        self.load_tasks()
        return self._task_index.get(task_id)

    def get_latest_entry(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recent history entry for a task."""
        task = self.get_task(task_id)
        if not task:
            return None
        history = task.get("history") or []
        if not history:
            return None
        return history[-1]

    def _rebuild_index(self) -> None:
        self._task_index = {}
        if not self._cache:
            return
        for task in self._cache:
            task_id = task.get("taskId")
            if task_id:
                self._task_index[task_id] = task


def _extract_result_preview(entry: Dict[str, Any]) -> Optional[str]:
    return (
        entry.get("resultPreview")
        or entry.get("result_preview")
        or entry.get("result_summary")
        or entry.get("result")
    )


def get_task_status(task_id: str, repository: TaskRepository) -> Optional[Dict[str, Any]]:
    """Return the latest status metadata for the requested task."""
    if not task_id:
        raise ValueError("get_task_status requires a task_id.")
    if not repository:
        raise ValueError("get_task_status requires a TaskRepository instance.")

    latest_entry = repository.get_latest_entry(task_id)
    if not latest_entry:
        return None

    return {
        "status": latest_entry.get("status"),
        "timestamp": latest_entry.get("timestamp"),
    }


def get_task_result(task_id: str, repository: TaskRepository) -> Optional[Dict[str, Any]]:
    """Return the completion result preview for the specified task."""
    if not task_id:
        raise ValueError("get_task_result requires a task_id.")
    if not repository:
        raise ValueError("get_task_result requires a TaskRepository instance.")

    latest_entry = repository.get_latest_entry(task_id)
    if not latest_entry or latest_entry.get("status") != "completed":
        return None

    result_preview = _extract_result_preview(latest_entry)
    if result_preview is None:
        return None

    return {
        "resultPreview": result_preview,
        "timestamp": latest_entry.get("timestamp"),
    }


@dataclass(frozen=True)
class TaskCompletionEvent:
    """Represents a task reaching a terminal status such as completed or failed."""

    task_id: str
    status: str
    timestamp: Optional[str]
    result_preview: Optional[str]


class TaskWatcher:
    """Poll ``tasks.json`` and emit events when tasks reach a terminal state."""

    TERMINAL_STATUSES: Tuple[str, ...] = ("completed", "failed")

    def __init__(self, repository: TaskRepository, interval_seconds: float = 2.0) -> None:
        if not repository:
            raise ValueError("TaskWatcher requires a TaskRepository instance.")
        self.repository = repository
        self.interval_seconds = float(interval_seconds)
        self._callbacks: List[Callable[[TaskCompletionEvent], None]] = []
        self._error_callbacks: List[Callable[[BaseException], None]] = []
        self._timer: Optional[threading.Timer] = None
        self._stop_event = threading.Event()
        self._last_seen_completion: Dict[str, Optional[str]] = {}
        self._lock = threading.Lock()
        self._logger = logging.getLogger("head-belya.task_watcher")

    def register_callback(self, callback: Callable[[TaskCompletionEvent], None]) -> None:
        """Register a callback for completion events."""
        if not callable(callback):
            raise ValueError("callback must be callable.")
        self._callbacks.append(callback)

    def register_error_callback(self, callback: Callable[[BaseException], None]) -> None:
        """Register a callback to receive polling exceptions."""
        if not callable(callback):
            raise ValueError("callback must be callable.")
        self._error_callbacks.append(callback)

    def start(self) -> None:
        """Start polling in the background."""
        with self._lock:
            if self._timer is not None:
                return
            self._stop_event.clear()

        try:
            self._poll(bootstrap=True)
        except Exception as exc:  # pragma: no cover - defensive
            self._emit_error(exc)

        self._logger.debug("TaskWatcher started with interval %.2fs", self.interval_seconds)
        self._schedule_next()

    def stop(self) -> None:
        """Stop polling for task updates."""
        self._stop_event.set()
        with self._lock:
            timer = self._timer
            self._timer = None
        if timer:
            timer.cancel()
        self._logger.debug("TaskWatcher stopped.")

    def _schedule_next(self) -> None:
        if self._stop_event.is_set():
            return
        timer = threading.Timer(self.interval_seconds, self._run_cycle)
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()

    def _run_cycle(self) -> None:
        try:
            self._poll()
        except Exception as exc:  # pragma: no cover - defensive
            self._emit_error(exc)
        finally:
            with self._lock:
                self._timer = None
        self._schedule_next()

    def _poll(self, bootstrap: bool = False) -> None:
        tasks = self.repository.load_tasks()
        for task in tasks:
            task_id = task.get("taskId")
            if not task_id:
                continue
            history = task.get("history") or []
            latest_entry = history[-1] if history else None
            if not latest_entry:
                self._last_seen_completion.setdefault(task_id, None)
                continue

            status = latest_entry.get("status")
            timestamp = latest_entry.get("timestamp")
            is_terminal = status in self.TERMINAL_STATUSES
            completion_signature = (
                f"{status}:{timestamp or 'unknown'}:{len(history)}" if is_terminal else None
            )

            previous_signature = self._last_seen_completion.get(task_id)
            self._last_seen_completion[task_id] = completion_signature

            is_new_completion = (
                not bootstrap
                and is_terminal
                and completion_signature
                and completion_signature != previous_signature
            )

            if is_new_completion:
                event = TaskCompletionEvent(
                    task_id=task_id,
                    status=status,
                    timestamp=timestamp,
                    result_preview=_extract_result_preview(latest_entry),
                )
                self._logger.debug(
                    "Detected terminal status '%s' for task %s",
                    status,
                    task_id,
                )
                self._emit_completion(event)

    def _emit_completion(self, event: TaskCompletionEvent) -> None:
        for callback in list(self._callbacks):
            try:
                callback(event)
            except Exception:  # pragma: no cover - defensive
                continue

    def _emit_error(self, error: BaseException) -> None:
        if not self._error_callbacks:
            self._logger.exception("Unhandled TaskWatcher error: %s", error)
            return
        for callback in list(self._error_callbacks):
            try:
                callback(error)
            except Exception:
                continue


def _usage_example() -> None:
    """Demonstrate the task utility helpers when run directly."""
    repo_path = Path(__file__).resolve().parent.parent / "tasks.json"
    repository = TaskRepository(str(repo_path))
    sample_ids = [
        "5ce72709262d4d9f931a218d9d287e86",
        "f7365c61b6b54d0193035769776faa47",
    ]

    logger.info("--- Manual task inspection demo ---")
    if SESSION_LOG_PATH:
        logger.info("Session log file: %s", SESSION_LOG_PATH)

    for task_id in sample_ids:
        status = get_task_status(task_id, repository)
        result = get_task_result(task_id, repository)
        logger.info("status for %s: %s", task_id, status)
        logger.info("result preview for %s: %s", task_id, result)

    def notify(event: TaskCompletionEvent) -> None:
        preview = event.result_preview or "<no preview>"
        logger.info("Task %s completed at %s · preview: %s", event.task_id, event.timestamp, preview)

    watcher = TaskWatcher(repository, interval_seconds=1.5)
    watcher.register_callback(notify)

    logger.info("Watching for new task completions for 5 seconds...")
    watcher.start()
    try:
        time.sleep(5)
    finally:
        watcher.stop()
        logger.info("Watcher stopped.")


class HeadBelyaAgent(AgentUtilitiesMixin, SessionManagementToolsMixin, Agent):
    """Supervisor agent coordinating sub-agents and owning session tools."""

    HEAD_AGENT_KEY = "head-belya"

    def __init__(self) -> None:
        self.session_store = SessionStore()
        self.codex_agent = CodexBelyaAgent()
        self.git_agent = GitBelyaAgent()
        self.rag_agent = RAGBelyaAgent()
        self.CodexAgent = self.codex_agent.CodexAgent
        self._sub_agents: Dict[str, Agent] = {
            "codex-belya": self.codex_agent,
            "git-belya": self.git_agent,
        }
        self._agent_aliases: Dict[str, str] = {}
        self.agent_tool_catalog: Dict[str, List[Dict[str, str]]] = {}
        self.task_manager = TaskManager()
        self._background_tasks: Dict[str, asyncio.Task[Any]] = {}
        tasks_file_path = self.task_manager.tasks_file
        resolved_tasks_path = (
            tasks_file_path.resolve()
            if isinstance(tasks_file_path, Path)
            else Path(tasks_file_path).resolve()
        )
        self._task_repository = TaskRepository(str(resolved_tasks_path))
        self._task_watcher = TaskWatcher(self._task_repository, interval_seconds=2.0)
        self._task_watcher.register_callback(self._handle_task_completion_event)
        self._task_watcher.register_error_callback(self._handle_task_watcher_error)
        self._watcher_started = False
        self._completion_event_queue: asyncio.Queue[TaskCompletionEvent] | None = None
        self._completion_processor: asyncio.Task[None] | None = None
        self._pending_completion_events: Deque[TaskCompletionEvent] = deque()
        self._asyncio_loop: asyncio.AbstractEventLoop | None = None

        self.sessions_ids_used = [
            record.session_id for record in self.session_store.list_sessions()
        ]
        self.utilization_warning_thresholds: Tuple[int, int, int] = (80, 90, 95)
        self.available_approval_policies: Tuple[str, ...] = ("never", "on-request", "on-failure", "untrusted")
        self.available_models: Tuple[str, ...] = ("gpt-5-codex", "gpt-5", "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini")
        self.session_settings_cache: Dict[str, Dict[str, Any]] = {}
        self.rate_limit_warning_cache: Dict[str, Dict[str, List[int]]] = {}
        self.livekit_state: Dict[str, Any] = self._load_livekit_state()
        super().__init__(
            instructions=(
                "Your name is Belya. You are a helpful voice assistant for Codex users. Your interface with users will be Voice. "
                "You help users in the following: "
                "1. collecting all the coding tasks they need from Codex to work on. Make sure you have all the needed work before sending it to Codex CLI. "
                "2. creating a single prompt for all the coding requests from the user to communicate to Codex. "
                "3. Get the code response, once Codex finish the task. "
                "4. reading out the code response to the user via voice; focusing on the task actions done and the list of tests communicated back from Codex. Do not read the diffs. "
                "You also have git control over the git repository you are working on. "
                "Ask the user if they have any more tasks to send to Codex, and repeat the process until the user is done. "
                "After their first task, ask them if they want to continue with the task or start a new one. use the 'start_a_new_session' function if they chose to start a new codex task. "
                "Any new session should have a different id than previous sessions. "
                "review the prompt with the user before sending it to the 'send_task_to_Codex' function. "
                "Always use the `send_task_to_Codex` tool to send any coding task to Codex CLI. "
                "Make sure you notify the user of the current branch before they start a new session/task. use the 'check_current_branch' to get the current branch. "
                "Ask the user if he wants to create a new branch and if the user approve, start a new branch in the repo before sending new tasks to Codex CLI. "
                "Do not change the branch mid-session. "
                "Ask the user if they have a preference for the branch name, and verify the branch name. use the 'create branch' tool. "
                "Never try to do any coding task by yourself. Do not ask the user to provide any code. "
                "Always wait for the Codex response before reading it out to the user. "
                "Be polite and professional. Sound excited to help the user. "
                "Coordinate codex-belya for coding tasks, git-belya for git operations, and rag-belya for repository research; do not execute those tasks yourself."
            ),
        )
        self._register_current_session()
        self._agent_aliases = self._build_agent_alias_map()
        self.refresh_agent_tool_catalog()

    def _handle_task_completion_event(self, event: TaskCompletionEvent) -> None:
        """Enqueue task completion events detected by the background watcher."""
        logger.debug(
            "TaskWatcher detected terminal status '%s' for task %s.",
            event.status,
            event.task_id,
        )
        if not self._asyncio_loop or not self._completion_event_queue:
            self._pending_completion_events.append(event)
            return

        def _enqueue() -> None:
            queue = self._completion_event_queue
            if queue is None:
                self._pending_completion_events.append(event)
                return
            queue.put_nowait(event)

        self._asyncio_loop.call_soon_threadsafe(_enqueue)

    def _handle_task_watcher_error(self, error: BaseException) -> None:
        """Log watcher exceptions without interrupting the main agent loop."""
        logger.exception("TaskWatcher reported an error: %s", error)

    def _ensure_task_watcher_started(self) -> None:
        """Initialise data structures and start the task watcher if needed."""
        if self._completion_event_queue is None:
            self._completion_event_queue = asyncio.Queue()
        if self._completion_processor is None or self._completion_processor.done():
            self._completion_processor = asyncio.create_task(self._process_completion_events())
        if not self._watcher_started:
            try:
                self._task_watcher.start()
                self._watcher_started = True
            except Exception as error:  # pragma: no cover - defensive
                logger.exception("Failed to start TaskWatcher: %s", error)
        while self._pending_completion_events and self._completion_event_queue is not None:
            event = self._pending_completion_events.popleft()
            self._completion_event_queue.put_nowait(event)

    async def _process_completion_events(self) -> None:
        """Process completion events serially inside the asyncio event loop."""
        queue = self._completion_event_queue
        if queue is None:
            return
        while True:
            primary_event = await queue.get()
            batch = [primary_event]
            try:
                while True:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await self._notify_user_of_task_completions(batch)
            except Exception as error:  # pragma: no cover - defensive
                logger.exception(
                    "Failed to notify user about completion batch %s: %s",
                    ", ".join(event.task_id for event in batch),
                    error,
                )
            finally:
                for _ in batch:
                    queue.task_done()

    async def _notify_user_of_task_completion(self, event: TaskCompletionEvent) -> None:
        """Backward-compatible wrapper for single completion notifications."""
        await self._notify_user_of_task_completions([event])

    async def _notify_user_of_task_completions(
        self,
        events: Sequence[TaskCompletionEvent],
    ) -> None:
        """Inform the user when one or more subordinate agent tasks finish."""
        if not events:
            return

        summaries: List[Dict[str, Any]] = []
        for event in events:
            task_record = self.task_manager.get_task(event.task_id) or {}
            agent_value = task_record.get("agent")
            canonical_agent = (
                self._canonicalize_agent_name(agent_value) if agent_value else agent_value
            )
            agent_label = canonical_agent or agent_value or "unknown agent"
            description = task_record.get("description") or "No description available."
            recorded_status = task_record.get("status")
            status_value = event.status or recorded_status or "completed"

            preview_source = event.result_preview or task_record.get("result")
            preview_text = None
            if isinstance(preview_source, str):
                preview_text = " ".join(preview_source.strip().split())
                if len(preview_text) > 360:
                    preview_text = preview_text[:357].rstrip() + "..."

            error_message = task_record.get("error")

            if status_value == "completed":
                status_phrase = "completed successfully"
            elif status_value == "failed":
                status_phrase = "failed"
            else:
                status_phrase = f"finished with status {status_value}"

            summaries.append(
                {
                    "task_id": event.task_id,
                    "agent_label": agent_label,
                    "description": description,
                    "status_phrase": status_phrase,
                    "status": status_value,
                    "preview": preview_text,
                    "error": error_message,
                    "timestamp": event.timestamp,
                }
            )

        message_parts = [
            "You are Belya, the supervising head agent.",
            "Provide the user with a single update summarizing only the following completed tasks.",
        ]

        for entry in summaries:
            entry_parts = [
                f"For task '{entry['task_id']}' handled by {entry['agent_label']}, {entry['status_phrase']}.",
                f"Task description: {entry['description']}.",
            ]
            if entry.get("preview"):
                entry_parts.append(f"Result summary: {entry['preview']}.")
            elif entry.get("error"):
                entry_parts.append(f"Reported error: {entry['error']}.")
            else:
                entry_parts.append("No result summary was provided.")
            if entry.get("timestamp"):
                entry_parts.append(f"Completion recorded at {entry['timestamp']}.")
            message_parts.append(" ".join(entry_parts))

        message_parts.append(
            "Offer to review any task in more detail or help with the next steps, but do not mention other tasks."
        )

        instructions = " ".join(message_parts)
        logger.info(
            "Notifying user about completion of tasks: %s",
            ", ".join(
                f"{entry['task_id']} ({entry['status']})" for entry in summaries
            ),
        )
        self.session.generate_reply(instructions=instructions)

    @function_tool
    async def send_task_to_Codex(self, task_prompt: str, run_ctx: RunContext) -> Optional[str]:
        """Delegate coding task execution to codex-belya and handle bookkeeping."""
        task_id = self._create_task_entry(
            agent_key="codex-belya",
            description=task_prompt,
            metadata={"prompt": task_prompt},
            start_note="Task dispatched to codex-belya",
        )
        self._dispatch_codex_task(task_id, task_prompt)
        return (
            f"Took note of your Codex request as task {task_id}. "
            "Codex is working on it now while I stay available for anything else you need."
        )

    @function_tool
    async def list_agent_tasks(
        self,
        agent_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> str:
        """Summarize tracked tasks, optionally filtered by agent or status."""
        canonical_agent = self._canonicalize_agent_name(agent_name)
        tasks = self.task_manager.get_tasks(agent=canonical_agent, status=status)
        if not tasks:
            return "No matching tasks found in the task log."
        summaries = []
        for entry in tasks:
            agent_label = entry.get("agent") or "unknown-agent"
            summaries.append(
                f"{entry.get('id')} [{agent_label}] ({entry.get('status')}): {entry.get('description')}"
            )
        return "Here are the tasks I'm tracking:\n" + "\n".join(summaries)

    @function_tool
    async def get_task_details(self, task_id: str) -> str:
        """Fetch the latest information about a specific task."""
        task = self.task_manager.get_task(task_id)
        if not task:
            return f"I couldn't find a task with id {task_id}."
        status = task.get("status")
        description = task.get("description")
        result = task.get("result")
        error = task.get("error")
        updates = task.get("history", [])
        update_summary = "; ".join(
            f"{item.get('timestamp')}: {item.get('note') or item.get('status')}"
            for item in updates
            if item
        )
        if status == "completed" and isinstance(result, str):
            return (
                f"Task {task_id} ({description}) is complete. "
                f"Here is the result:\n{result}"
                + (f"\nUpdates: {update_summary}" if update_summary else "")
            )
        if error:
            return (
                f"Task {task_id} ({description}) ended in status '{status}' with the following error: {error}"
                + (f"\nUpdates: {update_summary}" if update_summary else "")
            )
        return (
            f"Task {task_id} ({description}) is currently {status}."
            + (f"\nRecent updates: {update_summary}" if update_summary else "")
        )

    @function_tool
    async def start_head_task(self, description: str, note: Optional[str] = None) -> str:
        """Create and mark a head-belya task as in progress."""
        task_id = self._create_task_entry(
            agent_key=self.HEAD_AGENT_KEY,
            description=description,
            start_note=note or "Head-belya started this task",
        )
        return (
            f"Logged head-belya task {task_id}. "
            "I'll handle it and keep the tracker updated."
        )

    @function_tool
    async def add_head_task_note(self, task_id: str, note: str) -> str:
        """Append a progress note to a head-belya task without changing status."""
        if not note:
            return "Please provide a note so I can record it."
        task = self.task_manager.get_task(task_id)
        if not task:
            return f"I couldn't find a task with id {task_id}."
        if not self._task_assigned_to_head(task):
            return f"Task {task_id} is not currently assigned to head-belya."
        self.task_manager.append_task_note(task_id, note)
        return f"Added a new note to head-belya task {task_id}."

    @function_tool
    async def complete_head_task(
        self,
        task_id: str,
        result: Optional[str] = None,
        note: Optional[str] = None,
    ) -> str:
        """Mark a head-belya task as completed and optionally record a result."""
        task = self.task_manager.get_task(task_id)
        if not task:
            return f"I couldn't find a task with id {task_id}."
        if not self._task_assigned_to_head(task):
            return f"Task {task_id} is not currently assigned to head-belya."
        self.task_manager.update_task_status(
            task_id,
            "completed",
            note=note or "Head-belya completed this task",
            result=result,
        )
        return f"Marked head-belya task {task_id} as completed."

    @function_tool
    async def fail_head_task(
        self,
        task_id: str,
        error_message: str,
        note: Optional[str] = None,
    ) -> str:
        """Record that a head-belya task could not be finished."""
        if not error_message:
            return "Please provide an error message so I can log what went wrong."
        task = self.task_manager.get_task(task_id)
        if not task:
            return f"I couldn't find a task with id {task_id}."
        if not self._task_assigned_to_head(task):
            return f"Task {task_id} is not currently assigned to head-belya."
        self.task_manager.update_task_status(
            task_id,
            "failed",
            note=note or "Head-belya was unable to finish this task",
            error=error_message,
        )
        return f"Recorded a failure for head-belya task {task_id}."

    def _create_task_entry(
        self,
        *,
        agent_key: str,
        description: str,
        metadata: Optional[Dict[str, Any]] = None,
        start_note: Optional[str] = None,
        auto_start: bool = True,
    ) -> str:
        canonical_agent = self._canonicalize_agent_name(agent_key) or agent_key
        task_id = self.task_manager.add_task(
            agent=canonical_agent,
            description=description,
            metadata=metadata or {},
        )
        if auto_start:
            self.task_manager.update_task_status(
                task_id,
                "in_progress",
                note=start_note or f"{canonical_agent} started this task",
            )
        return task_id

    def _canonicalize_agent_name(self, agent_name: Optional[str]) -> Optional[str]:
        if not agent_name:
            return None
        resolved = self._resolve_agent_alias(agent_name)
        return resolved or agent_name

    def _task_assigned_to_head(self, task: Optional[Dict[str, Any]]) -> bool:
        if not task:
            return False
        agent_value = task.get("agent")
        canonical = self._canonicalize_agent_name(agent_value) if agent_value else None
        return canonical == self.HEAD_AGENT_KEY

    def _build_agent_alias_map(self) -> Dict[str, str]:
        """Construct a lowercase alias map for known agents for easy lookup."""
        aliases: Dict[str, str] = {
            "head-belya": "head-belya",
            "head": "head-belya",
            "belya": "head-belya",
            "supervisor": "head-belya",
            "voice-assistant": "head-belya",
        }
        for agent_key, agent in self._sub_agents.items():
            aliases[agent_key] = agent_key
            primary = agent_key.split("-", 1)[0]
            aliases[primary] = agent_key
            trimmed = agent_key.replace("-belya", "")
            aliases[trimmed] = agent_key
            aliases[agent.__class__.__name__] = agent_key
        return {alias.lower(): canonical for alias, canonical in aliases.items()}

    def _managed_agents(self) -> Dict[str, Agent]:
        """Return a map of agent identifiers to agent instances."""
        return {"head-belya": self, **self._sub_agents}

    def register_sub_agent(self, agent_key: str, agent: Agent) -> None:
        """Register or replace a subordinate agent and refresh discovery metadata."""
        if not agent_key:
            raise ValueError("agent_key must be provided when registering sub agents.")
        self._sub_agents[agent_key] = agent
        self._agent_aliases = self._build_agent_alias_map()
        self.refresh_agent_tool_catalog()

    def _resolve_agent_alias(self, agent_name: str) -> Optional[str]:
        """Normalize incoming agent names to a canonical identifier."""
        if not agent_name:
            return None
        return self._agent_aliases.get(agent_name.lower())

    def _discover_tools_for_agent(self, agent: Agent) -> List[Dict[str, str]]:
        """Inspect an agent for function tools exposed via @function_tool."""
        tool_entries: List[Dict[str, str]] = []
        for attr_name, attr_value in inspect.getmembers(agent.__class__, predicate=callable):
            tool_info = getattr(attr_value, "__livekit_tool_info", None)
            if not tool_info:
                continue
            bound_method = getattr(agent, attr_name)
            try:
                signature = str(inspect.signature(bound_method))
            except (TypeError, ValueError):
                signature = "()"
            docstring = inspect.getdoc(bound_method) or inspect.getdoc(attr_value) or ""
            description = tool_info.description or (docstring.splitlines()[0] if docstring else "")
            tool_name = tool_info.name or attr_name
            tool_entries.append(
                {
                    "name": str(tool_name),
                    "attribute": attr_name,
                    "signature": signature,
                    "description": description,
                    "doc": docstring,
                }
            )
        return sorted(tool_entries, key=lambda entry: entry["name"])

    def refresh_agent_tool_catalog(self) -> Dict[str, List[Dict[str, str]]]:
        """Recompute the cached mapping of agents to their available function tools."""
        catalog: Dict[str, List[Dict[str, str]]] = {}
        for agent_key, agent in self._managed_agents().items():
            catalog[agent_key] = self._discover_tools_for_agent(agent)
        self.agent_tool_catalog = catalog
        return catalog

    def _dispatch_codex_task(self, task_id: str, task_prompt: str) -> None:
        """Schedule the Codex task to run in the background."""
        async_task: asyncio.Task[None] = asyncio.create_task(self._run_codex_task(task_id, task_prompt))
        self._background_tasks[task_id] = async_task
        async_task.add_done_callback(lambda completed_task, tid=task_id: self._handle_background_completion(tid, completed_task))

    async def _run_codex_task(self, task_id: str, task_prompt: str) -> None:
        """Execute the Codex directive and update task state."""
        try:
            result: CodexTaskResult = await self.codex_agent.execute_directive(task_prompt)
            error_message = result.get("error")
            output_text = result.get("output", None)
            raw_result = result.get("raw_result")
            metadata_update: Dict[str, Any] = {}
            if raw_result is not None:
                metadata_update["raw_result_type"] = type(raw_result).__name__
                metadata_update["raw_result_preview"] = str(raw_result)[:500]
            if error_message:
                self.task_manager.update_task_status(
                    task_id,
                    "failed",
                    note="Codex reported an error",
                    error=error_message,
                    metadata_update=metadata_update,
                )
                return

            if output_text is None:
                output_text = self._extract_final_output(raw_result, task_prompt)
            self.task_manager.update_task_status(
                task_id,
                "completed",
                note="Codex task finished successfully",
                result=output_text,
                metadata_update=metadata_update,
            )
            warning_message = self._post_process_codex_activity(
                task_prompt,
                output_text,
                raw_result,
                entry_type="task",
            )
            if warning_message:
                self.task_manager.append_task_note(task_id, warning_message)
        except Exception as error:
            logger.exception("Unhandled error running Codex task %s", task_id)
            self.task_manager.update_task_status(
                task_id,
                "failed",
                note="Unhandled exception while executing Codex task",
                error=str(error),
            )

    def _handle_background_completion(self, task_id: str, background_task: asyncio.Task[Any]) -> None:
        """Cleanup bookkeeping when a background task finishes."""
        self._background_tasks.pop(task_id, None)
        if background_task.cancelled():
            self.task_manager.update_task_status(
                task_id,
                "failed",
                note="Codex task was cancelled unexpectedly",
                error="Task cancelled",
            )
            return
        exception = background_task.exception()
        if exception:
            logger.error(
                "Background Codex task %s raised an exception",
                task_id,
                exc_info=(exception.__class__, exception, exception.__traceback__),
            )
            self.task_manager.update_task_status(
                task_id,
                "failed",
                note="Codex task raised an exception after completion",
                error=str(exception),
            )

    @function_tool
    async def list_available_agent_functions(self, agent_name: Optional[str] = None) -> str:
        """Enumerate the function tools currently exposed by managed agents."""
        catalog = self.refresh_agent_tool_catalog()
        if agent_name:
            canonical = self._resolve_agent_alias(agent_name)
            if not canonical:
                known_agents = ", ".join(sorted(catalog.keys()))
                return (
                    f"I don't recognize an agent named {agent_name}. "
                    f"I currently manage the following agents: {known_agents}."
                )
            tools = catalog.get(canonical, [])
            if not tools:
                return f"{canonical} does not expose any function tools right now."
            lines = [
                f"- {entry['name']}{entry['signature']}: {entry['description']}"
                for entry in tools
            ]
            formatted = "\n".join(lines)
            return f"Here are the function tools available on {canonical}:\n{formatted}"

        segments = []
        for agent_key, tools in catalog.items():
            if tools:
                tool_list = ", ".join(entry["name"] for entry in tools)
            else:
                tool_list = "(no function tools registered)"
            segments.append(f"{agent_key}: {tool_list}")
        overview = "\n".join(segments)
        return f"I currently manage these agents and their function tools:\n{overview}"

    def _safe_get_current_branch(self) -> str | None:
        """Best-effort attempt to read the current git branch."""
        try:
            repo = self.git_agent._repo()
            return repo.active_branch.name
        except Exception as error:
            logger.warning("Unable to determine current branch: %s", error)
            return None

    def _register_current_session(self) -> None:
        """Ensure the active Codex session is tracked in the session store."""
        session_id = self._current_session_id()
        if not session_id:
            logger.warning("Codex agent session is missing a session_id; skipping registration.")
            return

        branch_name = self._safe_get_current_branch()
        try:
            record = self.session_store.ensure_session(session_id, branch_name)
        except Exception as error:
            logger.exception("Failed to register session %s: %s", session_id, error)
            record = None
        else:
            if session_id not in self.sessions_ids_used:
                self.sessions_ids_used.append(session_id)
        if record:
            settings = record.metadata.get("settings", {})
            normalized_settings = settings if isinstance(settings, dict) else {}
            self.session_settings_cache[session_id] = normalized_settings
            self._sync_codex_settings(normalized_settings)
            warnings = (
                record.metadata.get("metrics", {})
                .get("token_usage", {})
                .get("warnings", {})
            )
            if isinstance(warnings, dict):
                self.rate_limit_warning_cache[session_id] = {
                    "five_hour": list(warnings.get("five_hour", [])),
                    "weekly": list(warnings.get("weekly", [])),
                }

    def _update_current_session_branch(self, branch_name: str | None) -> None:
        """Persist the branch name for the active session."""
        if not branch_name:
            return
        session_id = self._current_session_id()
        if not session_id:
            return
        try:
            record = self.session_store.get_session(session_id)
            if record:
                self.session_store.update_branch(session_id, branch_name)
            else:
                self.session_store.ensure_session(session_id, branch_name)
        except Exception as error:
            logger.exception("Failed to update branch for session %s: %s", session_id, error)

    def _sync_codex_settings(self, settings: Dict[str, Any]) -> None:
        """Propagate stored settings to the Codex agent when available."""
        if not settings:
            return
        update_kwargs: Dict[str, Any] = {}
        if "approval_policy" in settings:
            update_kwargs["approval_policy"] = settings.get("approval_policy")
        if "model" in settings:
            update_kwargs["model"] = settings.get("model")
        if not update_kwargs:
            return
        try:
            self.codex_agent.update_settings(**update_kwargs)
        except Exception as error:
            logger.exception("Failed to sync Codex settings %s: %s", update_kwargs, error)

    def _get_session_record(self, session_id: Optional[str] = None) -> Optional[SessionRecord]:
        """Fetch a session record, defaulting to the active session."""
        session_lookup = session_id or self._current_session_id()
        if not session_lookup:
            return None
        try:
            return self.session_store.get_session(session_lookup)
        except Exception as error:
            logger.exception("Failed to load session record %s: %s", session_lookup, error)
            return None

    def _current_session_id(self) -> Optional[str]:
        return self.codex_agent.current_session_id()

    def _load_livekit_state(self) -> Dict[str, Any]:
        logger.debug("LiveKit state persistence is currently disabled; starting with an empty state.")
        return {}

    def _persist_livekit_state(self) -> None:
        logger.debug("LiveKit state persistence is disabled; skipping persistence request.")

    def get_livekit_state(self) -> Dict[str, Any]:
        return dict(self.livekit_state)

    def record_livekit_context(
        self,
        room_info: Optional[Dict[str, Any]] = None,
        participant_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        state_changed = False

        if room_info:
            for key in ("room_id", "room_sid", "room_name"):
                value = room_info.get(key)
                if value and self.livekit_state.get(key) != value:
                    self.livekit_state[key] = value
                    state_changed = True

        if participant_info:
            for key in ("participant_id", "participant_sid", "participant_identity"):
                value = participant_info.get(key)
                if value and self.livekit_state.get(key) != value:
                    self.livekit_state[key] = value
                    state_changed = True

        if state_changed:
            self.livekit_state["updated_at"] = self._current_time_iso()
            self._persist_livekit_state()
            logger.info(
                "Updated in-memory LiveKit context: room=%s participant=%s",
                self.livekit_state.get("room_sid") or self.livekit_state.get("room_name"),
                self.livekit_state.get("participant_sid") or self.livekit_state.get("participant_identity"),
            )

    async def _execute_codex_directive(self, directive: str, entry_type: str = "directive") -> str:
        result = await self.codex_agent.execute_directive(directive)
        output_text = result.get("output") or ""
        warning_message = self._post_process_codex_activity(directive, output_text, result.get("raw_result"), entry_type=entry_type)
        if warning_message:
            return f"{output_text}\n\n{warning_message}"
        return output_text

    @function_tool
    async def research_repository(self, question: str, run_ctx: RunContext, max_snippets: int = 5) -> str:
        """Delegate repository research to rag-belya."""
        return await self.rag_agent.research_repository(question=question, run_ctx=run_ctx, max_snippets=max_snippets)

    async def on_enter(self):
        self._asyncio_loop = asyncio.get_running_loop()
        self._ensure_task_watcher_started()
        self.session.generate_reply(
            instructions="greet the user and introduce yourself as Belya, a voice assistant for Codex users."
        )

    async def on_exit(self) -> None:
        if self._task_watcher:
            self._task_watcher.stop()
        if self._completion_processor:
            self._completion_processor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._completion_processor


def _create_git_delegate(tool_name: str):
    target = getattr(GitBelyaAgent, tool_name, None)
    if target is None:
        raise AttributeError(f"GitBelyaAgent has no tool named {tool_name}")

    target_callable = getattr(target, "__wrapped__", None) or getattr(target, "fn", None) or target
    target_signature = inspect.signature(target_callable)
    target_annotations = getattr(target_callable, "__annotations__", {})
    target_doc = getattr(target_callable, "__doc__", getattr(target, "__doc__", None))
    target_name = getattr(target_callable, "__name__", tool_name)

    async def _delegated(self, *args, **kwargs):
        method = getattr(self.git_agent, tool_name)
        return await method(*args, **kwargs)

    _delegated.__signature__ = target_signature
    _delegated.__annotations__ = dict(target_annotations)
    _delegated.__name__ = target_name
    _delegated.__doc__ = target_doc
    return function_tool(_delegated)


def _collect_function_tool_names(agent_cls: Type[Agent]) -> List[str]:
    """Return all @function_tool names declared on the given agent class."""
    tool_names: List[str] = []
    for attr_name, attr_value in inspect.getmembers(agent_cls, predicate=callable):
        if getattr(attr_value, "__livekit_tool_info", None):
            tool_names.append(attr_name)
    return tool_names


# Delegate each git-related function tool to git-belya so the supervisor remains the only entry point.
for _tool in _collect_function_tool_names(GitBelyaAgent):
    if hasattr(HeadBelyaAgent, _tool):
        continue
    setattr(HeadBelyaAgent, _tool, _create_git_delegate(_tool))


if __name__ == "__main__":
    _usage_example()
