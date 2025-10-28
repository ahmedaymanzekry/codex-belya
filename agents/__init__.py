"""Fallback implementations for the optional `agents` runtime.

The original project depends on the `agents` Python package, which is
not available in this execution environment. To keep the rest of the
application functional we provide a very small compatibility layer that
covers the subset of functionality used by ``mcp_server.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class Agent:
    """Minimal stand-in for the external Agent base class."""

    def __init__(
        self,
        *,
        name: str,
        instructions: str,
        mcp_servers: Optional[list[Any]] = None,
    ) -> None:
        self.name = name
        self.instructions = instructions
        self.mcp_servers: list[Any] = list(mcp_servers or [])


class SQLiteSession:
    """Lightweight session object used for persisting Codex state."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # Downstream code interacts with ``metadata``; keep it around.
        self.metadata: Dict[str, Any] = {}


@dataclass
class CodexRunResult:
    """Container that mimics the structure returned by the real runner."""

    final_output: str
    output: str = field(init=False)
    metrics: Dict[str, Any] = field(default_factory=dict)
    usage: Dict[str, Any] = field(default_factory=dict)
    usage_metrics: Dict[str, Any] = field(default_factory=dict)
    token_usage: Dict[str, Any] = field(default_factory=dict)
    rate_limits: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw_snapshot: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Downstream code checks both ``final_output`` and ``output``; keep
        # them in sync to avoid surprises.
        self.output = self.final_output


class Runner:
    """Fallback runner that returns a descriptive placeholder response."""

    @staticmethod
    async def run(
        agent: Agent,
        task_prompt: str,
        session: Optional[SQLiteSession] = None,
    ) -> CodexRunResult:
        logger.warning(
            "The optional 'agents' runtime is not installed; returning a placeholder Codex response."
        )
        message = (
            "Codex MCP support is unavailable in this environment. "
            "Install the official Codex MCP server dependencies to enable task execution."
        )
        metadata = {"prompt": task_prompt, "agent": getattr(agent, "name", "unknown")}
        return CodexRunResult(final_output=message, metadata=metadata)
