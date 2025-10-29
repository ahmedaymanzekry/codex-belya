import logging
from datetime import datetime
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RoomInputOptions,
    RoomOutputOptions,
    WorkerOptions,
    cli,
    metrics,
)
# from livekit.plugins.turn_detector.multilingual import MultilingualModel
from livekit.plugins import openai, silero
from livekit.plugins import noise_cancellation

from mcp_server import CodexCLIAgent, CodexCLISession
from session_store import SessionStore, SessionRecord

from git import Repo
from git.exc import GitCommandError
from tools import CodexTaskToolsMixin, GitFunctionToolsMixin, SessionManagementToolsMixin

import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("basic-agent")

load_dotenv()

class VoiceAssistantAgent(
    GitFunctionToolsMixin,
    SessionManagementToolsMixin,
    CodexTaskToolsMixin,
    Agent,
):
    def __init__(self) -> None:
        self.session_store = SessionStore()
        self.CodexAgent = CodexCLIAgent()
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
            instructions="Your name is Belya. You are a helpful voice assistant for Codex users. Your interface with users will be Voice.\
                You help users in the following:\
                1. collecting all the coding tasks they need from Codex to work on. Make sure you have all the needed work before sending it to Codex CLI.\
                2. creating a single prompt for all the coding requests from the user to communicate to Codex.\
                3. Get the code response, once Codex finish the task.\
                4. reading out the code response to the user via voice; focusing on the task actions done and the list of tests communicated back from Codex. Do not read the diffs.\
                You also have git control over the git repository you are working on.\
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

    def _current_time_iso(self) -> str:
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

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

    async def _execute_codex_directive(self, directive: str, entry_type: str = "directive") -> str:
        results = await self.CodexAgent.send_task(directive)
        output_text = self._extract_final_output(results, directive)
        warning_message = self._post_process_codex_activity(directive, output_text, results, entry_type=entry_type)
        if warning_message:
            return f"{output_text}\n\n{warning_message}"
        return output_text
    
    def _handle_tool_error(self, action: str, error: Exception) -> str:
        if isinstance(error, GitCommandError):
            details = (getattr(error, "stderr", "") or getattr(error, "stdout", "") or str(error)).strip()
            logger.exception("Git error while %s: %s", action, details or error)
            explanation = details or str(error)
            return f"I couldn't complete {action} because git reported: {explanation}"
        logger.exception(f"Error while {action}: {error}")
        return f"I ran into an error while {action}: {error}"
    
    async def on_enter(self):
        # when the agent is added to the session, it'll generate a reply
        # according to its instructions
        self.session.generate_reply(instructions="greet the user and introduce yourself as Belya, a voice assistant for Codex users.")

    
def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


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
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
