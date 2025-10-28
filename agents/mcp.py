"""Compatibility wrapper around the LiveKit MCP helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from livekit.agents.llm.mcp import MCPServerStdio as _LiveKitMCPServerStdio


class MCPServerStdio(_LiveKitMCPServerStdio):
    """Drop-in replacement for the original Agent MCP helper.

    The upstream project expected the constructor signature to accept a
    descriptive ``name`` and a ``params`` dictionary.  LiveKit exposes a
    slightly different API, so we adapt the arguments and provide the
    asynchronous context manager protocol that the rest of the codebase
    relies on.
    """

    def __init__(
        self,
        *,
        name: str,
        params: Dict[str, Any],
        client_session_timeout_seconds: float,
    ) -> None:
        command = params.get("command")
        if not command:
            raise ValueError("MCPServerStdio requires a 'command' entry in params")
        args = params.get("args", [])
        env: Optional[Dict[str, str]] = params.get("env")
        cwd = params.get("cwd")
        if isinstance(cwd, str):
            cwd = Path(cwd)

        super().__init__(
            command=command,
            args=list(args),
            env=env,
            cwd=cwd,
            client_session_timeout_seconds=client_session_timeout_seconds,
        )
        self.name = name
        self.params = params

    async def __aenter__(self) -> "MCPServerStdio":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()
