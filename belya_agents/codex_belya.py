from __future__ import annotations

from typing import Any, Optional

from livekit.agents import Agent

from .shared import AgentUtilitiesMixin
from mcp_server import CodexCLIAgent, CodexCLISession
from tools.codex_tools import CodexTaskResult, CodexTaskToolsMixin


class CodexBelyaAgent(AgentUtilitiesMixin, CodexTaskToolsMixin, Agent):
    """Codex specialist agent responsible for Codex CLI prompt execution."""

    def __init__(self, codex_client: Optional[CodexCLIAgent] = None) -> None:
        self.CodexAgent = codex_client or CodexCLIAgent()
        super().__init__(
            instructions=(
                "You are codex-belya. Handle Codex CLI tasks forwarded by head-belya. "
                "Only execute send_task_to_Codex; defer all other work to head-belya."
            )
        )

    def current_session(self) -> Optional[CodexCLISession]:
        """Expose the active Codex CLI session for the supervisor."""
        session = getattr(self.CodexAgent, "session", None)
        return session if isinstance(session, CodexCLISession) else None

    def current_session_id(self) -> Optional[str]:
        """Convenience accessor for the active session id."""
        session = self.current_session()
        return getattr(session, "session_id", None) if session else None

    def set_session(self, session: CodexCLISession) -> None:
        """Update the underlying Codex CLI session."""
        self.CodexAgent.session = session

    def update_settings(self, **kwargs: Any) -> None:
        """Proxy Codex CLI configuration updates."""
        update_callable = getattr(self.CodexAgent, "update_settings", None)
        if callable(update_callable):
            update_callable(**kwargs)

    async def execute_directive(self, directive: str) -> CodexTaskResult:
        """Execute a Codex CLI directive outside of the voice run context."""
        results = await self.CodexAgent.send_task(directive)
        output_text = self._extract_final_output(results, directive)
        return {
            "output": output_text,
            "raw_result": results,
        }
