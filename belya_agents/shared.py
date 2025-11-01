import logging
from datetime import datetime
from typing import Any

from git.exc import GitCommandError


class AgentUtilitiesMixin:
    """Shared utilities for Belya agents to keep behavior consistent across roles."""

    _logger = logging.getLogger("belya-agents")

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

    def _handle_tool_error(self, action: str, error: Exception) -> str:
        if isinstance(error, GitCommandError):
            details = (getattr(error, "stderr", "") or getattr(error, "stdout", "") or str(error)).strip()
            self._logger.exception("Git error while %s: %s", action, details or error)
            explanation = details or str(error)
            return f"I couldn't complete {action} because git reported: {explanation}"
        self._logger.exception("Error while %s: %s", action, error)
        return f"I ran into an error while {action}: {error}"

