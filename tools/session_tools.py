import json
import logging
import os
from typing import Any, Dict, List, Optional

from git import Repo
from livekit.agents import function_tool

from mcp_server import CodexCLISession
from .metrics_tools import SessionMetricsMixin


logger = logging.getLogger(__name__)


class SessionManagementToolsMixin(SessionMetricsMixin):
    """Mixin that exposes session management function tools."""

    @function_tool
    async def start_a_new_session(self, session_id: str) -> str:
        """Create and switch to a new Codex task session."""
        try:
            current_session_id = getattr(self.CodexAgent.session, "session_id", None)
            current_branch = self._safe_get_current_branch()

            if current_session_id:
                try:
                    self.session_store.ensure_session(current_session_id, current_branch)
                except Exception as store_error:
                    logger.exception(
                        "Failed to persist metadata for session %s: %s",
                        current_session_id,
                        store_error,
                    )
                if current_session_id not in self.sessions_ids_used:
                    self.sessions_ids_used.append(current_session_id)

            if self.session_store.session_exists(session_id):
                logger.info("Session id %s has been used before.", session_id)
                return (
                    f"The session id {session_id} has been used before. "
                    "Please provide a different session id for the new Codex task session."
                )

            if session_id in self.sessions_ids_used:
                logger.info("Session id %s was used earlier in this runtime.", session_id)
                return (
                    f"The session id {session_id} has already been used in this runtime. "
                    "Please choose a different session id."
                )

            self.CodexAgent.session = CodexCLISession(session_id=session_id)
            new_record = None
            try:
                new_record = self.session_store.ensure_session(session_id, current_branch)
            except Exception as store_error:
                logger.exception("Failed to register new session %s: %s", session_id, store_error)
            else:
                self.sessions_ids_used.append(session_id)
                settings = new_record.metadata.get("settings", {}) if new_record else {}
                self.session_settings_cache[session_id] = settings if isinstance(settings, dict) else {}
                self._sync_codex_settings(self.session_settings_cache[session_id])
                warnings = (
                    new_record.metadata.get("metrics", {})
                    .get("token_usage", {})
                    .get("warnings", {})
                    if new_record
                    else {}
                )
                if isinstance(warnings, dict):
                    self.rate_limit_warning_cache[session_id] = {
                        "five_hour": list(warnings.get("five_hour", [])),
                        "weekly": list(warnings.get("weekly", [])),
                    }
                else:
                    self.rate_limit_warning_cache[session_id] = {"five_hour": [], "weekly": []}
            logger.info("Started a new Codex agent session.")
            return "Started a new Codex task session. Please provide the new coding task you want Codex to work on."
        except Exception as error:
            return self._handle_tool_error("starting a new Codex session", error)

    @function_tool
    async def list_past_sessions(self) -> str:
        """List all recorded Codex sessions with their metadata."""
        try:
            sessions = self.session_store.list_sessions()
            if not sessions:
                return "I don't have any recorded Codex sessions yet."

            formatted_sessions = []
            for record in sessions:
                tasks = record.metadata.get("tasks", [])
                branch = record.branch_name or "unknown branch"
                metrics = record.metadata.get("metrics", {})
                token_usage = metrics.get("token_usage", {}) if isinstance(metrics, dict) else {}
                total_tokens = token_usage.get("total_tokens")
                tokens_summary = f", tokens used: {total_tokens}" if total_tokens is not None else ""
                formatted_sessions.append(
                    f"{record.session_id} (branch: {branch}, last used: {record.updated_at}, "
                    f"tasks logged: {len(tasks)}{tokens_summary})"
                )

            session_overview = "; ".join(formatted_sessions)
            logger.info("Listing stored Codex sessions: %s", session_overview)
            return f"Here are the Codex sessions I've recorded: {session_overview}."
        except Exception as error:
            return self._handle_tool_error("listing past sessions", error)

    @function_tool
    async def check_current_session(self) -> str:
        """Return the active Codex session id and related metadata."""
        try:
            session_id = getattr(self.CodexAgent.session, "session_id", None)
            if not session_id:
                logger.info("No active Codex session found when checking current session.")
                return "There is no active Codex session at the moment."

            record = self.session_store.get_session(session_id)
            if not record:
                branch = self._safe_get_current_branch() or "unknown branch"
                logger.info(
                    "Active session %s not found in store. Branch fallback: %s",
                    session_id,
                    branch,
                )
                return (
                    f"The active Codex session id is {session_id}, but I don't have stored metadata for it yet. "
                    f"The current git branch appears to be {branch}."
                )

            branch = record.branch_name or "unknown branch"
            tasks_logged = len(record.metadata.get("tasks", []))
            metrics_summary = self._format_usage_summary(record.metadata.get("metrics", {}))
            logger.info(
                "Current session %s metadata requested. Branch: %s, tasks logged: %s, last used: %s",
                session_id,
                branch,
                tasks_logged,
                record.updated_at,
            )
            return (
                f"The active Codex session id is {session_id}. "
                f"It last worked on {branch} and was updated at {record.updated_at}. "
                f"I have {tasks_logged} task entries recorded for this session. "
                f"{metrics_summary}"
            )
        except Exception as error:
            return self._handle_tool_error("checking the current session", error)

    @function_tool
    async def switch_session(self, session_id: str) -> str:
        """Switch to an existing Codex session by its session id."""
        try:
            current_session_id = getattr(self.CodexAgent.session, "session_id", None)
            if session_id == current_session_id:
                logger.info("Requested to switch to the current session %s.", session_id)
                return f"We are already using the Codex session {session_id}."

            record = self.session_store.get_session(session_id)
            if not record:
                logger.info("Attempted to switch to unknown session id %s.", session_id)
                return f"I couldn't find a saved Codex session with the id {session_id}."

            self.CodexAgent.session = CodexCLISession(session_id=session_id)
            try:
                self.session_store.ensure_session(session_id, record.branch_name)
            except Exception as store_error:
                logger.exception(
                    "Failed to refresh session metadata for %s: %s",
                    session_id,
                    store_error,
                )

            if session_id not in self.sessions_ids_used:
                self.sessions_ids_used.append(session_id)

            settings = record.metadata.get("settings", {})
            self.session_settings_cache[session_id] = settings if isinstance(settings, dict) else {}
            self._sync_codex_settings(self.session_settings_cache[session_id])
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

            stored_branch = record.branch_name
            current_branch = self._safe_get_current_branch()
            branch_notice = ""
            if stored_branch and current_branch and stored_branch != current_branch:
                branch_notice = (
                    f" Please note that this session previously worked on branch {stored_branch}, "
                    f"but the repository is currently on {current_branch}."
                )
            elif stored_branch and (not current_branch or stored_branch == current_branch):
                branch_notice = f" This session previously worked on branch {stored_branch}."
            elif not stored_branch and current_branch:
                branch_notice = (
                    f" I don't have stored branch information for this session, and the repository is currently on {current_branch}."
                )
            else:
                branch_notice = " I couldn't determine the branch information for this session."

            logger.info("Switched to Codex session %s.", session_id)
            return f"Switched to Codex session {session_id}.{branch_notice}"
        except Exception as error:
            return self._handle_tool_error("switching Codex sessions", error)

    @function_tool
    async def set_session_branch(self, branch_name: str) -> str:
        """Record or update the branch associated with the active Codex session."""
        try:
            session_id = getattr(self.CodexAgent.session, "session_id", None)
            if not session_id:
                logger.info("User requested to set session branch but there is no active session.")
                return "There is no active Codex session to update right now."

            record = self.session_store.get_session(session_id)
            previous_branch = record.branch_name if record else None
            if previous_branch == branch_name:
                logger.info(
                    "Branch %s is already associated with session %s.",
                    branch_name,
                    session_id,
                )
                return (
                    f"The session {session_id} is already associated with branch {branch_name}. "
                    "Let me know if you need anything else."
                )

            repo = Repo(os.getcwd())
            available_branches = [head.name for head in repo.heads]
            if branch_name not in available_branches:
                logger.info(
                    "Attempted to set branch %s for session %s, but it is not a known local branch.",
                    branch_name,
                    session_id,
                )
                return (
                    f"I couldn't find a local branch named {branch_name}. "
                    "Please provide an existing branch name or create it first."
                )

            self._update_current_session_branch(branch_name)
            if session_id not in self.sessions_ids_used:
                self.sessions_ids_used.append(session_id)

            if previous_branch and previous_branch != branch_name:
                logger.info(
                    "Updated session %s branch from %s to %s.",
                    session_id,
                    previous_branch,
                    branch_name,
                )
                return (
                    f"Updated the current Codex session {session_id} to track branch {branch_name} "
                    f"instead of {previous_branch}."
                )

            logger.info("Recorded branch %s for session %s.", branch_name, session_id)
            return (
                f"Recorded branch {branch_name} for the current Codex session {session_id}. "
                "Let me know if you need anything else."
            )
        except Exception as error:
            return self._handle_tool_error("recording the session branch", error)

    @function_tool
    async def get_session_metrics(self, session_id: str | None = None) -> str:
        """Provide token utilization metrics for a specific or current Codex session."""
        try:
            record = self._get_session_record(session_id)
            if not record:
                target = session_id or "current"
                logger.info("Requested metrics for unknown session %s.", target)
                return f"I couldn't find utilization metrics for the session id {target}."

            metrics = record.metadata.get("metrics", {})
            summary = self._format_usage_summary(metrics)
            return f"Utilization for session {record.session_id}: {summary}"
        except Exception as error:
            return self._handle_tool_error("retrieving Codex session utilization metrics", error)

    @function_tool
    async def list_sessions_utilization(self) -> str:
        """Summarize utilization metrics for all stored Codex sessions."""
        try:
            sessions = self.session_store.list_sessions()
            if not sessions:
                return "I don't have any recorded Codex sessions yet."

            summaries = []
            for record in sessions:
                metrics = record.metadata.get("metrics", {})
                usage_summary = self._format_usage_summary(metrics)
                summaries.append(f"{record.session_id}: {usage_summary}")

            return " ".join(summaries)
        except Exception as error:
            return self._handle_tool_error("listing Codex session utilization metrics", error)

    @function_tool
    async def get_rate_limit_status(self, session_id: str | None = None) -> str:
        """Report the current rate limit status for the requested Codex session."""
        try:
            record = self._get_session_record(session_id)
            if not record:
                target = session_id or "current"
                logger.info("Requested rate limit status for unknown session %s.", target)
                return f"I couldn't find rate limit details for the session id {target}."

            metrics = record.metadata.get("metrics", {})
            status = self._format_rate_limit_status(metrics)
            return f"Codex rate limit status for session {record.session_id}: {status}"
        except Exception as error:
            return self._handle_tool_error("retrieving Codex rate limit information", error)

    @function_tool
    async def compact_codex_session(self) -> str:
        """Send a /compact directive to Codex to reduce session context."""
        try:
            if not self._current_session_id():
                return "There is no active Codex session to compact right now."
            response = await self._execute_codex_directive("/compact", entry_type="directive")
            return f"Codex responded to the compaction request: {response}"
        except Exception as error:
            return self._handle_tool_error("requesting Codex context compaction", error)

    @function_tool
    async def rename_codex_session(self, new_session_id: str) -> str:
        """Rename the active Codex session id per user request."""
        try:
            current_session_id = self._current_session_id()
            if not current_session_id:
                return "There is no active Codex session to rename right now."

            proposed_id = new_session_id.strip()
            if not proposed_id:
                return "Please provide a non-empty session id to rename to."
            if proposed_id == current_session_id:
                return f"The Codex session is already using the id {proposed_id}."
            if self.session_store.session_exists(proposed_id):
                return (
                    f"The session id {proposed_id} is already in use. "
                    "Please choose a different name."
                )

            rename_success = self.session_store.rename_session(current_session_id, proposed_id)
            if not rename_success:
                logger.info(
                    "Failed to rename session %s to %s in the store.",
                    current_session_id,
                    proposed_id,
                )
                return f"I couldn't rename the session to {proposed_id}. Please try a different name."

            agent_rename = getattr(self.CodexAgent, "rename_session", None)
            if callable(agent_rename):
                agent_rename(proposed_id)
            else:
                self.CodexAgent.session = CodexCLISession(session_id=proposed_id)

            if current_session_id in self.sessions_ids_used:
                self.sessions_ids_used = [
                    proposed_id if sid == current_session_id else sid for sid in self.sessions_ids_used
                ]
            else:
                self.sessions_ids_used.append(proposed_id)

            settings_cache = self.session_settings_cache.pop(current_session_id, {})
            if settings_cache is not None:
                self.session_settings_cache[proposed_id] = settings_cache
                self._sync_codex_settings(settings_cache)

            warning_cache = self.rate_limit_warning_cache.pop(current_session_id, {"five_hour": [], "weekly": []})
            self.rate_limit_warning_cache[proposed_id] = warning_cache

            try:
                self.session_store.append_entry(
                    proposed_id,
                    prompt=f"Session renamed from {current_session_id} to {proposed_id}",
                    result=None,
                    entry_type="session_rename",
                )
            except Exception as store_error:
                logger.exception("Failed to log session rename for %s: %s", proposed_id, store_error)

            logger.info("Renamed Codex session from %s to %s.", current_session_id, proposed_id)
            return (
                f"Renamed the active Codex session from {current_session_id} to {proposed_id}. "
                "Future Codex tasks will continue in the renamed session."
            )
        except Exception as error:
            return self._handle_tool_error("renaming the Codex session", error)

    @function_tool
    async def configure_codex_session(
        self,
        approval_policy: str | None = None,
        model: str | None = None,
    ) -> str:
        """Update Codex session approval policy or model selections."""
        try:
            session_id = self._current_session_id()
            if not session_id:
                return "There is no active Codex session to configure right now."

            current_settings = self.session_settings_cache.get(session_id, {})
            settings_update: Dict[str, Any] = {}
            messages: List[str] = []

            if approval_policy is not None:
                if approval_policy not in self.available_approval_policies:
                    options = ", ".join(self.available_approval_policies)
                    return (
                        f"{approval_policy} is not a supported approval policy. "
                        f"Please choose one of the following options: {options}."
                    )
                settings_update["approval_policy"] = approval_policy
                messages.append(f"approval policy set to {approval_policy}")

            if model is not None:
                if model not in self.available_models:
                    options = ", ".join(self.available_models)
                    return (
                        f"{model} is not a supported Codex model target. "
                        f"Please choose one of the following options: {options}."
                    )
                settings_update["model"] = model
                messages.append(f"model set to {model}")

            if not settings_update:
                current_policy = current_settings.get("approval_policy", self.available_approval_policies[0])
                current_model = current_settings.get("model", self.available_models[0])
                policy_options = ", ".join(self.available_approval_policies)
                model_options = ", ".join(self.available_models)
                return (
                    f"The current Codex settings are approval policy '{current_policy}' and model '{current_model}'. "
                    f"Supported approval policies: {policy_options}. Supported models: {model_options}. "
                    "Let me know which ones you would like to switch to."
                )

            updated_settings = dict(current_settings)
            updated_settings.update(settings_update)
            self.session_settings_cache[session_id] = updated_settings
            self._sync_codex_settings(updated_settings)

            try:
                self.session_store.update_settings(session_id, settings_update)
            except Exception as store_error:
                logger.exception(
                    "Failed to persist Codex session settings for %s: %s",
                    session_id,
                    store_error,
                )

            try:
                self.session_store.append_entry(
                    session_id,
                    prompt=f"Session settings updated: {json.dumps(settings_update)}",
                    result=None,
                    entry_type="configuration",
                )
            except Exception as store_error:
                logger.exception(
                    "Failed to log Codex session settings change for %s: %s",
                    session_id,
                    store_error,
                )

            summary = "; ".join(messages)
            policy_options = ", ".join(self.available_approval_policies)
            model_options = ", ".join(self.available_models)
            return (
                f"Updated Codex session settings: {summary}. "
                f"Available approval policies: {policy_options}. Available models: {model_options}."
            )
        except Exception as error:
            return self._handle_tool_error("configuring Codex session settings", error)
