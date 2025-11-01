from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, List, Optional, Tuple

from livekit.agents import Agent, RunContext, function_tool

from .codex_belya import CodexBelyaAgent
from .git_belya import GitBelyaAgent
from .shared import AgentUtilitiesMixin
from session_store import SessionRecord, SessionStore
from tools.codex_tools import CodexTaskResult
from tools.session_tools import SessionManagementToolsMixin

logger = logging.getLogger("head-belya")


class HeadBelyaAgent(AgentUtilitiesMixin, SessionManagementToolsMixin, Agent):
    """Supervisor agent coordinating sub-agents and owning session tools."""

    def __init__(self) -> None:
        self.session_store = SessionStore()
        self.codex_agent = CodexBelyaAgent()
        self.git_agent = GitBelyaAgent()
        self.CodexAgent = self.codex_agent.CodexAgent

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
                "Coordinate codex-belya for coding tasks and git-belya for git operations; do not execute those tasks yourself."
            ),
        )
        self._register_current_session()

    @function_tool
    async def send_task_to_Codex(self, task_prompt: str, run_ctx: RunContext) -> Optional[str]:
        """Delegate coding task execution to codex-belya and handle bookkeeping."""
        result: CodexTaskResult = await self.codex_agent.send_task_to_Codex(task_prompt, run_ctx)
        error_message = result.get("error")
        if error_message:
            return error_message

        output_text = result.get("output")
        raw_result = result.get("raw_result")
        if isinstance(output_text, str):
            warning_message = self._post_process_codex_activity(
                task_prompt,
                output_text,
                raw_result,
                entry_type="task",
            )
            if warning_message:
                return f"{output_text}\n\n{warning_message}"
            return output_text
        return None

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

    async def on_enter(self):
        self.session.generate_reply(
            instructions="greet the user and introduce yourself as Belya, a voice assistant for Codex users."
        )


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


# Delegate each git-related function tool to git-belya so the supervisor remains the only entry point.
GIT_TOOL_NAMES = [
    "status",
    "add",
    "diff",
    "restore",
    "reset",
    "stash",
    "merge",
    "mv",
    "rm",
    "clean",
    "check_current_branch",
    "create_branch",
    "commit_changes",
    "pull_updates",
    "fetch_updates",
    "list_branches",
    "delete_branch",
    "push_branch",
    "switch_branch",
]

for _tool in GIT_TOOL_NAMES:
    setattr(HeadBelyaAgent, _tool, _create_git_delegate(_tool))
