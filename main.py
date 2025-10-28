import asyncio
import logging
import math
from datetime import datetime
import json
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RoomInputOptions,
    RoomOutputOptions,
    RunContext,
    WorkerOptions,
    cli,
    metrics,
    function_tool,
)
# from livekit.plugins.turn_detector.multilingual import MultilingualModel
from livekit.plugins import openai, silero
from livekit.plugins import noise_cancellation

from mcp_server import CodexCLIAgent, CodexCLISession
from session_store import SessionStore, SessionRecord
from web_server import ensure_web_app_started

from git import Repo

import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("basic-agent")

load_dotenv()

class VoiceAssistantAgent(Agent):
    def __init__(self) -> None:
        self.session_store = SessionStore()
        self.CodexAgent = CodexCLIAgent()
        self.sessions_ids_used = [
            record.session_id for record in self.session_store.list_sessions()
        ]
        self.utilization_warning_thresholds: Tuple[int, int, int] = (80, 90, 95)
        self.available_approval_policies: Tuple[str, ...] = ("never", "on-request", "on-failure", "untrusted")
        self.available_models: Tuple[str, ...] = ("gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini")
        self.session_settings_cache: Dict[str, Dict[str, Any]] = {}
        self.rate_limit_warning_cache: Dict[str, Dict[str, List[int]]] = {}
        self.livekit_state: Dict[str, Any] = self._load_livekit_state()
        super().__init__(
            instructions="Your name is Belya. You are a helpful voice assistant for Codex users. Your interface with users will be Voice.\
                You help users in the following:\
                1. collecting all the coding tasks they need from Codex to work on. Make sure you have all the needed work before sending it to Codex CLI.\
                2. creating a single prompt for all the coding requests from the user to communicate to Codex.\
                3. Get the code response, once Codex finish the task.\
                4. reading out the code response to the user via voice; focusing on the task actions done and the list of tests communicated back from Codex. Do not read the diffs.\
                Ask the user if they have any more tasks to send to Codex, and repeat the process until the user is done.\
                After their first task, ask them if they want to continue with the task or start a new one. use the 'start_a_new_session' function if they chose to start a new codex task. \
                Any new session should have a different id than previous sessions.\
                review the prompt with the user before sending it to the 'send_task_to_Codex' function. \
                Always use the `send_task_to_Codex` tool to send any coding task to Codex CLI.\
                Make sure you notify the user of the current branch before they start a new session/task. use the 'check_current_branch' to get the current branch.\
                Ask the user if he wants to create a new branch and if the user approve, start a new branch in the repo before sending new tasks to Codex CLI.\
                Do not change the branch mid-session.\
                Ask the user if they have a preference for the branch name, and verify the branch name. use the 'create branch' tool.\
                Never try to do any coding task by yourself. Do not ask the user to provide any code.\
                Always wait for the Codex response before reading it out to the user.\
                Be polite and professional. Sound excited to help the user.",
    )
        self._register_current_session()
    
    def _safe_get_current_branch(self) -> str | None:
        """Best-effort attempt to read the current git branch."""
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            return repo.active_branch.name
        except Exception as error:
            logger.warning(f"Unable to determine current branch: {error}")
            return None

    def _register_current_session(self) -> None:
        """Ensure the active Codex session is tracked in the session store."""
        session_id = getattr(self.CodexAgent.session, "session_id", None)
        if not session_id:
            logger.warning("Codex agent session is missing a session_id; skipping registration.")
            return

        branch_name = self._safe_get_current_branch()
        try:
            record = self.session_store.ensure_session(session_id, branch_name)
        except Exception as error:
            logger.exception(f"Failed to register session {session_id}: {error}")
            record = None
        else:
            if session_id not in self.sessions_ids_used:
                self.sessions_ids_used.append(session_id)
        if record:
            settings = record.metadata.get("settings", {})
            self.session_settings_cache[session_id] = settings
            self._sync_codex_settings(settings)
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
        session_id = getattr(self.CodexAgent.session, "session_id", None)
        if not session_id:
            return
        try:
            record = self.session_store.get_session(session_id)
            if record:
                self.session_store.update_branch(session_id, branch_name)
            else:
                self.session_store.ensure_session(session_id, branch_name)
        except Exception as error:
            logger.exception(f"Failed to update branch for session {session_id}: {error}")

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
        update_callable = getattr(self.CodexAgent, "update_settings", None)
        if callable(update_callable):
            try:
                update_callable(**update_kwargs)
            except Exception as error:
                logger.exception(f"Failed to sync Codex settings {update_kwargs}: {error}")
        else:
            logger.debug("Codex agent does not expose update_settings; skipping sync.")

    def _get_session_record(self, session_id: Optional[str] = None) -> Optional[SessionRecord]:
        """Fetch a session record, defaulting to the active session."""
        session_lookup = session_id or getattr(self.CodexAgent.session, "session_id", None)
        if not session_lookup:
            return None
        try:
            return self.session_store.get_session(session_lookup)
        except Exception as error:
            logger.exception(f"Failed to load session record {session_lookup}: {error}")
            return None

    def _current_session_id(self) -> Optional[str]:
        return getattr(self.CodexAgent.session, "session_id", None)

    def _load_livekit_state(self) -> Dict[str, Any]:
        try:
            state = self.session_store.get_livekit_state()
            if isinstance(state, dict):
                return state
        except Exception as error:
            logger.exception(f"Failed to load stored LiveKit state: {error}")
        return {}

    def _persist_livekit_state(self) -> None:
        try:
            self.session_store.set_livekit_state(self.livekit_state)
        except Exception as error:
            logger.exception(f"Failed to persist LiveKit state: {error}")

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
                "Stored LiveKit context for reuse: room=%s participant=%s",
                self.livekit_state.get("room_sid") or self.livekit_state.get("room_name"),
                self.livekit_state.get("participant_sid") or self.livekit_state.get("participant_identity"),
            )

    def _current_time_iso(self) -> str:
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    def _estimate_tokens(self, *texts: Optional[str]) -> int:
        combined = " ".join(text for text in texts if text)
        if not combined:
            return 0
        estimated = math.ceil(len(combined) / 4)
        return max(int(estimated), 0)

    def _to_plain_dict(self, value: Any) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        for converter in ("model_dump", "dict", "to_dict"):
            method = getattr(value, converter, None)
            if callable(method):
                try:
                    plain = method()
                    if isinstance(plain, dict):
                        return plain
                except Exception:
                    continue
        data = getattr(value, "__dict__", None)
        if isinstance(data, dict):
            simplified: Dict[str, Any] = {}
            for key, item in data.items():
                if key.startswith("_"):
                    continue
                if isinstance(item, (str, int, float, bool, dict, list, tuple, type(None))):
                    simplified[key] = item
            if simplified:
                return simplified
        return None

    def _merge_dicts(self, base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base)
        for key, value in updates.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
            ):
                merged[key] = self._merge_dicts(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _flatten_numeric_entries(self, data: Any, prefix: str = "") -> List[Tuple[str, float]]:
        entries: List[Tuple[str, float]] = []
        if isinstance(data, dict):
            for key, value in data.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                entries.extend(self._flatten_numeric_entries(value, path))
        elif isinstance(data, (list, tuple)):
            for index, item in enumerate(data):
                path = f"{prefix}[{index}]" if prefix else f"[{index}]"
                entries.extend(self._flatten_numeric_entries(item, path))
        elif isinstance(data, (int, float)):
            entries.append((prefix, float(data)))
        return entries

    def _find_numeric_by_terms(
        self,
        entries: List[Tuple[str, float]],
        term_sets: List[Tuple[str, ...]],
    ) -> Optional[float]:
        for terms in term_sets:
            for path, value in entries:
                path_lower = path.lower()
                if all(term in path_lower for term in terms):
                    return value
        return None

    def _extract_usage_metrics(
        self,
        codex_result: Any,
        prompt: str,
        output_text: str,
    ) -> Dict[str, Any]:
        payloads: List[Dict[str, Any]] = []
        rate_limits_payload: Dict[str, Any] = {}

        for attr_name in ("metrics", "usage", "usage_metrics", "token_usage", "rate_limits", "metadata"):
            attr = getattr(codex_result, attr_name, None)
            plain = self._to_plain_dict(attr)
            if plain:
                payloads.append(plain)
                if attr_name == "rate_limits":
                    rate_limits_payload = self._merge_dicts(rate_limits_payload, plain)

        combined: Dict[str, Any] = {}
        for payload in payloads:
            combined = self._merge_dicts(combined, payload)

        flat_entries = self._flatten_numeric_entries(combined)

        total_tokens = self._find_numeric_by_terms(
            flat_entries,
            [
                ("total", "token"),
                ("token", "total"),
            ],
        )
        delta_tokens = self._find_numeric_by_terms(
            flat_entries,
            [
                ("delta", "token"),
                ("token", "delta"),
                ("tokens", "used"),
                ("used", "token"),
            ],
        )
        five_hour_used = self._find_numeric_by_terms(
            flat_entries,
            [
                ("five", "hour", "used"),
                ("5", "hour", "used"),
                ("five-hour", "used"),
            ],
        )
        five_hour_limit = self._find_numeric_by_terms(
            flat_entries,
            [
                ("five", "hour", "limit"),
                ("5", "hour", "limit"),
                ("five-hour", "limit"),
            ],
        )
        five_hour_remaining = self._find_numeric_by_terms(
            flat_entries,
            [
                ("five", "hour", "remaining"),
                ("5", "hour", "remaining"),
                ("five-hour", "remaining"),
            ],
        )
        weekly_used = self._find_numeric_by_terms(
            flat_entries,
            [
                ("week", "used"),
                ("weekly", "used"),
            ],
        )
        weekly_limit = self._find_numeric_by_terms(
            flat_entries,
            [
                ("week", "limit"),
                ("weekly", "limit"),
            ],
        )
        weekly_remaining = self._find_numeric_by_terms(
            flat_entries,
            [
                ("week", "remaining"),
                ("weekly", "remaining"),
            ],
        )

        metrics: Dict[str, Any] = {}
        if total_tokens is not None:
            metrics["total_tokens"] = int(total_tokens)
        if delta_tokens is not None:
            metrics["delta_tokens"] = int(delta_tokens)

        window_metrics: Dict[str, Dict[str, Any]] = {}
        if five_hour_used is not None or five_hour_limit is not None or five_hour_remaining is not None:
            window_metrics["five_hour"] = {
                "used": int(five_hour_used) if five_hour_used is not None else None,
                "limit": int(five_hour_limit) if five_hour_limit is not None else None,
                "remaining": int(five_hour_remaining) if five_hour_remaining is not None else None,
                "last_updated": self._current_time_iso(),
            }

        if weekly_used is not None or weekly_limit is not None or weekly_remaining is not None:
            window_metrics["weekly"] = {
                "used": int(weekly_used) if weekly_used is not None else None,
                "limit": int(weekly_limit) if weekly_limit is not None else None,
                "remaining": int(weekly_remaining) if weekly_remaining is not None else None,
                "last_updated": self._current_time_iso(),
            }

        if window_metrics:
            metrics.update(window_metrics)

        if rate_limits_payload:
            metrics["rate_limits"] = rate_limits_payload

        if combined:
            metrics["raw_snapshot"] = combined

        if "delta_tokens" not in metrics:
            metrics["delta_tokens"] = self._estimate_tokens(prompt, output_text)

        if "total_tokens" not in metrics:
            metrics["total_tokens"] = metrics["delta_tokens"]

        return metrics

    def _prepare_metrics_update(
        self,
        session_id: str,
        prompt: str,
        output_text: str,
        codex_result: Any,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        extracted = self._extract_usage_metrics(codex_result, prompt, output_text)
        existing_metrics = self.session_store.get_metrics(session_id) or {}
        token_usage_existing = existing_metrics.get("token_usage", {})

        delta_tokens = extracted.get("delta_tokens")
        if delta_tokens is None:
            delta_tokens = self._estimate_tokens(prompt, output_text)
        try:
            delta_tokens_int = max(int(delta_tokens), 0)
        except (TypeError, ValueError):
            delta_tokens_int = self._estimate_tokens(prompt, output_text)

        reported_total = extracted.get("total_tokens")
        try:
            reported_total_int = int(reported_total) if reported_total is not None else None
        except (TypeError, ValueError):
            reported_total_int = None

        previous_total = token_usage_existing.get("total_tokens")
        try:
            previous_total_int = int(previous_total) if previous_total is not None else 0
        except (TypeError, ValueError):
            previous_total_int = 0

        new_total_tokens = reported_total_int if reported_total_int is not None else previous_total_int + delta_tokens_int

        token_usage_update: Dict[str, Any] = {
            "total_tokens": new_total_tokens,
        }

        for window_key in ("five_hour", "weekly"):
            window_existing = token_usage_existing.get(window_key, {}) if isinstance(token_usage_existing, dict) else {}
            window_extracted = extracted.get(window_key, {})
            if not isinstance(window_existing, dict):
                window_existing = {}
            if not isinstance(window_extracted, dict):
                window_extracted = {}

            window_update: Dict[str, Any] = {}
            for field in ("used", "limit", "remaining", "last_updated"):
                field_value = window_extracted.get(field)
                if field_value is not None:
                    window_update[field] = field_value

            if "used" not in window_update and window_existing.get("used") is not None:
                try:
                    window_update["used"] = int(window_existing.get("used")) + delta_tokens_int
                except (TypeError, ValueError):
                    window_update["used"] = window_existing.get("used")

            if window_update:
                merged_window = dict(window_existing)
                merged_window.update(window_update)
                merged_window.setdefault("last_updated", self._current_time_iso())
                token_usage_update[window_key] = merged_window

        metrics_update: Dict[str, Any] = {
            "token_usage": token_usage_update,
            "last_task_tokens": delta_tokens_int,
        }

        rate_limits = extracted.get("rate_limits")
        if isinstance(rate_limits, dict) and rate_limits:
            metrics_update["rate_limits"] = rate_limits

        raw_snapshot = extracted.get("raw_snapshot")
        if isinstance(raw_snapshot, dict) and raw_snapshot:
            metrics_update["last_snapshot"] = raw_snapshot

        entry_extra: Dict[str, Any] = {
            "tokens_used": delta_tokens_int,
        }
        if rate_limits:
            entry_extra["rate_limits"] = rate_limits

        return metrics_update, entry_extra

    def _extract_final_output(self, codex_result: Any, fallback_prompt: str = "") -> str:
        if hasattr(codex_result, "final_output"):
            candidate = getattr(codex_result, "final_output")
            if isinstance(candidate, str):
                return candidate
        if hasattr(codex_result, "output"):
            candidate = getattr(codex_result, "output")
            if isinstance(candidate, str):
                return candidate
        if isinstance(codex_result, str):
            return codex_result
        return (
            f"I got some results for Codex task working on the prompt {fallback_prompt}. "
            f"Here are the details: {codex_result}"
        )

    def _refresh_warning_cache(self, session_id: str) -> None:
        record = self._get_session_record(session_id)
        if not record:
            return
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

    def _maybe_emit_usage_warnings(self, session_id: str) -> Optional[str]:
        record = self._get_session_record(session_id)
        if not record:
            return None
        token_usage = (
            record.metadata.get("metrics", {})
            .get("token_usage", {})
        )
        if not isinstance(token_usage, dict):
            return None

        warnings_cache = self.rate_limit_warning_cache.setdefault(
            session_id,
            {"five_hour": [], "weekly": []},
        )

        messages: List[str] = []
        for window_key, label in (("five_hour", "5-hour"), ("weekly", "weekly")):
            window_data = token_usage.get(window_key, {})
            if not isinstance(window_data, dict):
                continue
            limit = window_data.get("limit")
            used = window_data.get("used")

            try:
                limit_int = int(limit) if limit is not None else None
                used_int = int(used) if used is not None else None
            except (TypeError, ValueError):
                limit_int = None
                used_int = None

            if not limit_int or not used_int or limit_int <= 0:
                continue

            percent = (used_int / limit_int) * 100
            triggered_levels = warnings_cache.setdefault(window_key, [])

            for threshold in self.utilization_warning_thresholds:
                if percent >= threshold and threshold not in triggered_levels:
                    try:
                        self.session_store.record_usage_warning(session_id, window_key, threshold)
                    except Exception as error:
                        logger.exception(f"Failed to persist usage warning {threshold}% for {window_key}: {error}")
                    triggered_levels.append(threshold)
                    messages.append(
                        f"Warning: Codex {label} token usage reached {percent:.1f}% "
                        f"({used_int} of {limit_int} tokens)."
                    )
                    break

        if messages:
            return " ".join(messages)
        return None

    def _format_usage_summary(self, metrics: Dict[str, Any]) -> str:
        token_usage = metrics.get("token_usage", {}) if isinstance(metrics, dict) else {}
        total_tokens = token_usage.get("total_tokens")
        summary_parts = []
        if total_tokens is not None:
            summary_parts.append(f"Total tokens used: {total_tokens}.")
        last_tokens = metrics.get("last_task_tokens")
        if last_tokens is not None:
            summary_parts.append(f"Last task consumed approximately {last_tokens} tokens.")

        for window_key, label in (("five_hour", "5-hour"), ("weekly", "weekly")):
            window = token_usage.get(window_key, {})
            if not isinstance(window, dict):
                continue
            limit = window.get("limit")
            used = window.get("used")
            remaining = window.get("remaining")
            if limit and used:
                try:
                    percent = (int(used) / int(limit)) * 100 if int(limit) else None
                except (TypeError, ValueError, ZeroDivisionError):
                    percent = None
                if percent is not None:
                    remaining_text = f", {int(remaining)} remaining" if remaining is not None else ""
                    summary_parts.append(
                        f"{label} window usage: {int(used)} of {int(limit)} tokens "
                        f"({percent:.1f}% used{remaining_text})."
                    )
            elif used:
                summary_parts.append(f"{label} window usage: {int(used)} tokens consumed.")

        if not summary_parts:
            return "I do not have token utilization metrics for this session yet."
        return " ".join(summary_parts)

    def _format_rate_limit_status(self, metrics: Dict[str, Any]) -> str:
        token_usage = metrics.get("token_usage", {}) if isinstance(metrics, dict) else {}
        lines = []
        for window_key, label in (("five_hour", "5-hour"), ("weekly", "weekly")):
            window = token_usage.get(window_key, {})
            if not isinstance(window, dict):
                lines.append(f"{label.capitalize()} utilization details are not available yet.")
                continue
            limit = window.get("limit")
            used = window.get("used")
            remaining = window.get("remaining")
            if limit and used:
                try:
                    percent = (int(used) / int(limit)) * 100 if int(limit) else None
                except (TypeError, ValueError, ZeroDivisionError):
                    percent = None
                if percent is not None:
                    remaining_text = f" with {int(remaining)} tokens remaining" if remaining is not None else ""
                    lines.append(
                        f"{label.capitalize()} window usage is at {percent:.1f}% "
                        f"({int(used)} of {int(limit)} tokens used{remaining_text})."
                    )
                    continue
            lines.append(f"{label.capitalize()} utilization details are not available yet.")
        if not lines:
            return "I do not have rate limit information for this session."
        return " ".join(lines)

    def _post_process_codex_activity(
        self,
        prompt: str,
        output_text: str,
        codex_result: Any,
        entry_type: str = "task",
    ) -> Optional[str]:
        session_id = getattr(self.CodexAgent.session, "session_id", None)
        if not session_id:
            return None

        try:
            metrics_update, entry_extra = self._prepare_metrics_update(session_id, prompt, output_text, codex_result)
        except Exception as error:
            logger.exception(f"Failed to prepare metrics update for session {session_id}: {error}")
            metrics_update = None
            entry_extra = None

        if metrics_update:
            try:
                self.session_store.update_metrics(session_id, metrics_update)
            except Exception as error:
                logger.exception(f"Failed to update metrics for session {session_id}: {error}")

        try:
            self.session_store.append_entry(
                session_id,
                prompt=prompt,
                result=output_text,
                entry_type=entry_type,
                extra=entry_extra,
            )
        except Exception as error:
            logger.exception(f"Failed to persist Codex activity for session {session_id}: {error}")

        self._refresh_warning_cache(session_id)
        return self._maybe_emit_usage_warnings(session_id)

    async def _execute_codex_directive(self, directive: str, entry_type: str = "directive") -> str:
        results = await self.CodexAgent.send_task(directive)
        output_text = self._extract_final_output(results, directive)
        warning_message = self._post_process_codex_activity(directive, output_text, results, entry_type=entry_type)
        if warning_message:
            return f"{output_text}\n\n{warning_message}"
        return output_text
    
    def _handle_tool_error(self, action: str, error: Exception) -> str:
        logger.exception(f"Error while {action}: {error}")
        return f"I ran into an error while {action}: {error}"
    
    async def on_enter(self):
        # when the agent is added to the session, it'll generate a reply
        # according to its instructions
        self.session.generate_reply(instructions="greet the user and introduce yourself as Belya, a voice assistant for Codex users.")

    @function_tool
    async def check_current_branch(self) -> str:
        """Called when user wants to know the current branch in the repo."""
        try:
            repo_path = os.getcwd()  # assuming the current working directory is the repo path
            repo = Repo(repo_path)
            current_branch = repo.active_branch.name
            logger.info(f"Current branch in repo at {repo_path} is {current_branch}.")
            return f"Current branch in the repo is {current_branch}."
        except Exception as error:
            return self._handle_tool_error("checking the current branch", error)
    
    @function_tool
    async def create_branch(self, branch_name: str) -> str:
        """Called when user wants to create a new branch in the repo for Codex to work on.
        Args:
            branch_name: The name of the new branch to be created.
        """
        try:
            repo_path = os.getcwd()  # assuming the current working directory is the repo path
            repo = Repo(repo_path)
            if branch_name in [head.name for head in repo.heads]:
                logger.info(f"Branch {branch_name} already exists in repo at {repo_path}.")
                return f"The branch {branch_name} already exists. Please pick a different name or switch to it."
            # create new branch
            repo.git.checkout("HEAD", b=branch_name)
            logger.info(f"Created and checked out new branch {branch_name} in repo at {repo_path}.")
            return f"Created and checked out new branch {branch_name} in the repo."
        except Exception as error:
            return self._handle_tool_error("creating a new branch", error)

    @function_tool
    async def commit_changes(self, commit_message: str) -> str:
        """Called when user wants to commit all current changes with a message.
        Args:
            commit_message: The commit message to use.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            if not repo.is_dirty(untracked_files=True):
                logger.info(f"No changes to commit in repo at {repo_path}.")
                return "There are no changes to commit."

            repo.git.add(all=True)
            commit = repo.index.commit(commit_message)
            logger.info(
                f"Committed changes in repo at {repo_path} with message '{commit_message}'. Commit id: {commit.hexsha}."
            )
            return f"Committed changes with message: {commit_message}."
        except Exception as error:
            return self._handle_tool_error("committing changes", error)

    @function_tool
    async def pull_updates(self, remote_name: str = "origin", branch_name: str | None = None) -> str:
        """Called when user wants to pull the latest updates from the remote branch.
        Args:
            remote_name: The name of the remote to pull from. Defaults to 'origin'.
            branch_name: The name of the branch to pull. Defaults to the current branch.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            active_branch = repo.active_branch.name
            branch_to_pull = branch_name or active_branch
            remote = repo.remote(remote_name)
            pull_infos = remote.pull(branch_to_pull)
            summaries = ", ".join(
                info.summary for info in pull_infos if hasattr(info, "summary") and info.summary
            )
            logger.info(
                f"Pulled updates from {remote_name}/{branch_to_pull} in repo at {repo_path}. Summaries: {summaries}"
            )
            if not summaries:
                summaries = "Pull completed with no additional details."
            return f"Pulled latest updates from {remote_name}/{branch_to_pull}. {summaries}"
        except Exception as error:
            return self._handle_tool_error("pulling updates", error)

    @function_tool
    async def fetch_updates(self, remote_name: str = "origin") -> str:
        """Called when user wants to fetch updates from the remote without merging."""
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            remote = repo.remote(remote_name)
            fetch_infos = remote.fetch()
            summaries = ", ".join(
                info.summary for info in fetch_infos if hasattr(info, "summary") and info.summary
            )
            logger.info(
                f"Fetched updates from {remote_name} in repo at {repo_path}. Summaries: {summaries}"
            )
            if not summaries:
                summaries = "Fetch completed with no additional details."
            return f"Fetched updates from {remote_name}. {summaries}"
        except Exception as error:
            return self._handle_tool_error("fetching updates", error)

    @function_tool
    async def list_branches(self) -> str:
        """Called when user wants to list all local branches."""
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            branches = [head.name for head in repo.heads]
            current_branch = repo.active_branch.name

            if not branches:
                logger.info(f"No branches found in repo at {repo_path}.")
                return "No branches found in the repository."

            formatted_branches = [
                f"{name} (current)" if name == current_branch else name for name in branches
            ]
            branch_list = ", ".join(formatted_branches)
            logger.info(f"Listed branches in repo at {repo_path}: {branch_list}")
            return f"The local branches are: {branch_list}."
        except Exception as error:
            return self._handle_tool_error("listing branches", error)

    @function_tool
    async def delete_branch(self, branch_name: str, force: bool = False) -> str:
        """Called when user wants to delete a local branch.
        Args:
            branch_name: The name of the branch to delete.
            force: Whether to force delete the branch even if it is not fully merged.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            current_branch = repo.active_branch.name
            if branch_name == current_branch:
                logger.info(
                    f"Attempted to delete current branch {branch_name} in repo at {repo_path}."
                )
                return "Cannot delete the branch you are currently on. Please switch to another branch first."

            if branch_name not in [head.name for head in repo.heads]:
                logger.info(f"Attempted to delete non-existent branch {branch_name} in repo at {repo_path}.")
                return f"The branch {branch_name} does not exist."

            flag = "-D" if force else "-d"
            repo.git.branch(flag, branch_name)
            logger.info(
                f"Deleted branch {branch_name} in repo at {repo_path} with force={force}."
            )
            return f"Deleted branch {branch_name}."
        except Exception as error:
            return self._handle_tool_error("deleting the branch", error)

    @function_tool
    async def push_branch(
        self, remote_name: str = "origin", branch_name: str | None = None, set_upstream: bool | None = None
    ) -> str:
        """Called when user wants to push the current or specified branch to a remote.
        Args:
            remote_name: The remote to push to. Defaults to 'origin'.
            branch_name: The branch to push. Defaults to the current branch.
            set_upstream: Force setting upstream; if None it is auto-detected.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            remote = repo.remote(remote_name)

            active_branch = repo.active_branch
            branch_to_push = branch_name or active_branch.name

            if branch_to_push not in [head.name for head in repo.heads]:
                logger.info(
                    f"Attempted to push non-existent branch {branch_to_push} in repo at {repo_path}."
                )
                return f"The branch {branch_to_push} does not exist locally."

            head_ref = next(head for head in repo.heads if head.name == branch_to_push)
            tracking_branch = head_ref.tracking_branch()

            should_set_upstream = set_upstream if set_upstream is not None else tracking_branch is None

            if should_set_upstream:
                push_result = remote.push(f"{branch_to_push}:{branch_to_push}", set_upstream=True)
            else:
                push_result = remote.push(branch_to_push)

            summaries = ", ".join(
                info.summary for info in push_result if hasattr(info, "summary") and info.summary
            )
            logger.info(
                f"Pushed branch {branch_to_push} to {remote_name} from repo at {repo_path}. "
                f"Set upstream: {should_set_upstream}. Summaries: {summaries}"
            )
            if not summaries:
                summaries = "Push completed with no additional details."

            upstream_msg = (
                "Upstream branch configured."
                if should_set_upstream
                else "Used existing upstream."
            )
            return f"Pushed {branch_to_push} to {remote_name}. {upstream_msg} {summaries}"
        except Exception as error:
            return self._handle_tool_error("pushing the branch", error)

    @function_tool
    async def switch_branch(self, branch_name: str) -> str:
        """Called when user wants to switch to an existing branch.
        Args:
            branch_name: The name of the branch to switch to.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            if branch_name not in [head.name for head in repo.heads]:
                logger.info(
                    f"Attempted to switch to non-existent branch {branch_name} in repo at {repo_path}."
                )
                return f"The branch {branch_name} does not exist."

            repo.git.checkout(branch_name)
            logger.info(f"Switched to branch {branch_name} in repo at {repo_path}.")
            return f"Switched to branch {branch_name}."
        except Exception as error:
            return self._handle_tool_error("switching branches", error)

    @function_tool
    async def start_a_new_session(self, session_id: str) -> str:
        """Called when user wants to start a new Codex task session."""
        try:
            current_session_id = getattr(self.CodexAgent.session, "session_id", None)
            current_branch = self._safe_get_current_branch()

            if current_session_id:
                try:
                    self.session_store.ensure_session(current_session_id, current_branch)
                except Exception as store_error:
                    logger.exception(f"Failed to persist metadata for session {current_session_id}: {store_error}")
                if current_session_id not in self.sessions_ids_used:
                    self.sessions_ids_used.append(current_session_id)

            if self.session_store.session_exists(session_id):
                logger.info(f"Session id {session_id} has been used before. Asking user for a different session id.")
                return (
                    f"The session id {session_id} has been used before. "
                    "Please provide a different session id for the new Codex task session."
                )

            if session_id in self.sessions_ids_used:
                logger.info(f"Session id {session_id} was used in the current runtime. Asking for a different id.")
                return (
                    f"The session id {session_id} has already been used in this runtime. "
                    "Please choose a different session id."
                )

            self.CodexAgent.session = CodexCLISession(session_id=session_id)
            new_record: Optional[SessionRecord] = None
            try:
                new_record = self.session_store.ensure_session(session_id, current_branch)
            except Exception as store_error:
                logger.exception(f"Failed to register new session {session_id}: {store_error}")
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
                    f"{record.session_id} (branch: {branch}, last used: {record.updated_at}, tasks logged: {len(tasks)}{tokens_summary})"
                )

            session_overview = "; ".join(formatted_sessions)
            logger.info(f"Listing stored Codex sessions: {session_overview}")
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
                logger.info(f"Active session {session_id} not found in store. Branch fallback: {branch}")
                return (
                    f"The active Codex session id is {session_id}, but I don't have stored metadata for it yet. "
                    f"The current git branch appears to be {branch}."
                )

            branch = record.branch_name or "unknown branch"
            tasks_logged = len(record.metadata.get("tasks", []))
            metrics_summary = self._format_usage_summary(record.metadata.get("metrics", {}))
            logger.info(
                f"Current session {session_id} metadata requested. Branch: {branch}, tasks logged: {tasks_logged}, last used: {record.updated_at}"
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
                logger.info(f"Requested to switch to the current session {session_id}.")
                return f"We are already using the Codex session {session_id}."

            record = self.session_store.get_session(session_id)
            if not record:
                logger.info(f"Attempted to switch to unknown session id {session_id}.")
                return f"I couldn't find a saved Codex session with the id {session_id}."

            self.CodexAgent.session = CodexCLISession(session_id=session_id)
            try:
                self.session_store.ensure_session(session_id, record.branch_name)
            except Exception as store_error:
                logger.exception(f"Failed to refresh session metadata for {session_id}: {store_error}")

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

            logger.info(f"Switched to Codex session {session_id}. Branch note: {branch_notice.strip()}")
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
                    f"Branch {branch_name} is already associated with session {session_id}; no update required."
                )
                return (
                    f"The session {session_id} is already associated with branch {branch_name}. "
                    "Let me know if you need anything else."
                )

            repo_path = os.getcwd()
            repo = Repo(repo_path)
            available_branches = [head.name for head in repo.heads]
            if branch_name not in available_branches:
                logger.info(
                    f"Attempted to set branch {branch_name} for session {session_id}, but it is not a known local branch."
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
                    f"Updated session {session_id} branch from {previous_branch} to {branch_name}."
                )
                return (
                    f"Updated the current Codex session {session_id} to track branch {branch_name} "
                    f"instead of {previous_branch}."
                )

            logger.info(f"Recorded branch {branch_name} for session {session_id}.")
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
                logger.info(f"Requested metrics for unknown session {target}.")
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
                logger.info(f"Requested rate limit status for unknown session {target}.")
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
                logger.info(f"Failed to rename session {current_session_id} to {proposed_id} in the store.")
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
                logger.exception(f"Failed to log session rename for {proposed_id}: {store_error}")

            logger.info(f"Renamed Codex session from {current_session_id} to {proposed_id}.")
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
                logger.exception(f"Failed to persist Codex session settings for {session_id}: {store_error}")

            try:
                self.session_store.append_entry(
                    session_id,
                    prompt=f"Session settings updated: {json.dumps(settings_update)}",
                    result=None,
                    entry_type="configuration",
                )
            except Exception as store_error:
                logger.exception(f"Failed to log Codex session settings change for {session_id}: {store_error}")

            summary = "; ".join(messages)
            policy_options = ", ".join(self.available_approval_policies)
            model_options = ", ".join(self.available_models)
            return (
                f"Updated Codex session settings: {summary}. "
                f"Available approval policies: {policy_options}. Available models: {model_options}."
            )
        except Exception as error:
            return self._handle_tool_error("configuring Codex session settings", error)

    @function_tool
    async def send_task_to_Codex(self, task_prompt: str, run_ctx: RunContext) -> str | None:
        """Called when user asks to send a task prompt to Codex.
        Args:
            task_prompt: The prompt text describing the task to be sent to Codex CLI.
            run_ctx: The run context for this function call.
        """
        try:
            logger.info(f"Sending the following task prompt to Codex CLI {task_prompt}.")

            # wait for the task to finish or the agent speech to be interrupted
            # alternatively, you can disallow interruptions for this function call with
            run_ctx.disallow_interruptions()

            wait_for_result = asyncio.ensure_future(self._a_long_running_task(task_prompt))
            try:
                await run_ctx.speech_handle.wait_if_not_interrupted([wait_for_result])
            except Exception:
                wait_for_result.cancel()
                raise

            if run_ctx.speech_handle.interrupted:
                logger.info(f"Interrupted receiving reply from Codex task with prompt {task_prompt}")
                # return None to skip the tool reply
                wait_for_result.cancel()
                return None

            result_bundle = wait_for_result.result()
            output_text = result_bundle.get("output") if isinstance(result_bundle, dict) else result_bundle
            raw_result = result_bundle.get("raw_result") if isinstance(result_bundle, dict) else None
            if not isinstance(output_text, str):
                output_text = self._extract_final_output(raw_result, task_prompt)
            logger.info(f"Done receiving Codex reply for the task with prompt {task_prompt}, result: {output_text}")
            warning_message = self._post_process_codex_activity(
                task_prompt,
                output_text,
                raw_result,
                entry_type="task",
            )
            if warning_message:
                output_text = f"{output_text}\n\n{warning_message}"
            return output_text
        except Exception as error:
            return self._handle_tool_error("sending the task to Codex", error)

    async def _a_long_running_task(self, task_prompt: str) -> Dict[str, Any]:
        """Simulate a long running task."""
        results = await self.CodexAgent.send_task(task_prompt)
        output_text = self._extract_final_output(results, task_prompt)
        logger.info(f"Finished long running Codex task for prompt {task_prompt}.")
        return {
            "output": output_text,
            "raw_result": results,
        }
    
def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    ensure_web_app_started()


async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    session = AgentSession(
        # turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        # any combination of STT, LLM, TTS, or realtime API can be used
        stt=openai.STT(),
        llm=openai.LLM(),
        tts=openai.TTS(instructions="Use a friendly and professional tone of voice. Be cheerful and encouraging. Sound excited to help the user."),
        # allow the LLM to generate a response while waiting for the end of turn
        # See more at https://docs.livekit.io/agents/build/audio/#preemptive-generation
        preemptive_generation=True,
        # sometimes background noise could interrupt the agent session, these are considered false positive interruptions
        # when it's detected, you may resume the agent's speech
        resume_false_interruption=True,
        false_interruption_timeout=1.0,
    )

    # log metrics as they are emitted, and total usage after session is over
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info(f"Usage: {summary}")

    # shutdown callbacks are triggered when the session is over
    ctx.add_shutdown_callback(log_usage)

    voice_agent = VoiceAssistantAgent()
    stored_livekit_state = voice_agent.get_livekit_state()

    stored_room_sid = stored_livekit_state.get("room_sid") or stored_livekit_state.get("room_id")
    if stored_room_sid:
        ctx.log_context_fields["stored_room_sid"] = stored_room_sid

    room_input_options = RoomInputOptions(
        noise_cancellation=noise_cancellation.BVC(),
    )
    room_output_options = RoomOutputOptions(transcription_enabled=True)

    participant_hint = (
        stored_livekit_state.get("participant_identity")
        or stored_livekit_state.get("participant_sid")
        or stored_livekit_state.get("participant_id")
    )

    if participant_hint:
        for attr in ("identity", "participant_identity"):
            if hasattr(room_input_options, attr):
                setattr(room_input_options, attr, participant_hint)
                break
        for attr in ("identity", "participant_identity"):
            if hasattr(room_output_options, attr):
                setattr(room_output_options, attr, participant_hint)
                break

    token_claims = None
    try:
        token_claims = ctx.token_claims()
    except Exception as error:
        logger.warning("Unable to decode worker token claims: %s", error)

    initial_room_info = {
        "room_id": getattr(ctx.room, "sid", None) or getattr(ctx.room, "name", None),
        "room_sid": getattr(ctx.room, "sid", None),
        "room_name": getattr(ctx.room, "name", None),
    }
    voice_agent.record_livekit_context(initial_room_info, None)

    if token_claims:
        agent_participant = None
        try:
            agent_participant = getattr(ctx, "agent", None)
        except Exception:
            agent_participant = None

        preflight_room_info = {
            "room_id": getattr(ctx.room, "sid", None),
            "room_sid": getattr(ctx.room, "sid", None),
            "room_name": getattr(getattr(ctx, "room", None), "name", None)
            or getattr(getattr(token_claims, "video", None), "room", None),
        }
        preflight_participant_info = {
            "participant_id": getattr(agent_participant, "sid", None),
            "participant_sid": getattr(agent_participant, "sid", None),
            "participant_identity": getattr(token_claims, "identity", None),
        }
        if any(preflight_room_info.values()) or any(preflight_participant_info.values()):
            voice_agent.record_livekit_context(preflight_room_info, preflight_participant_info)

    try:
        await session.start(
            agent=voice_agent,
            room=ctx.room,
            room_input_options=room_input_options,
            room_output_options=room_output_options,
        )
    finally:
        room_obj = getattr(ctx, "room", None)
        room_info = {
            "room_id": getattr(room_obj, "sid", None) or getattr(room_obj, "name", None),
            "room_sid": getattr(room_obj, "sid", None),
            "room_name": getattr(room_obj, "name", None),
        }

        agent_participant = getattr(session, "agent_participant", None)
        if agent_participant is None:
            agent_participant = getattr(session, "participant", None)

        participant_info = {
            "participant_id": getattr(agent_participant, "sid", None) or getattr(agent_participant, "identity", None),
            "participant_sid": getattr(agent_participant, "sid", None),
            "participant_identity": getattr(agent_participant, "identity", None),
        }

        voice_agent.record_livekit_context(room_info, participant_info)

if __name__ == "__main__":
    ensure_web_app_started()
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
