import logging
from typing import Any, Dict, Optional

from agents import Agent, Runner, SQLiteSession
from agents.mcp import MCPServerStdio

logger = logging.getLogger(__name__)

class CodexMCPServer(MCPServerStdio):
    def __init__(self, enable_search: bool = False) -> None:
        args = ["-y", "codex", "mcp-server"]
        if enable_search:
            args = ["-y", "codex", "--search", "mcp-server"]
        super().__init__(
            name="Codex MCP Server",
            params={
                "command": "npx",
                "args": args,
                },
            client_session_timeout_seconds=360000,
        )

class CodexMCPAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            name="Codex MCP Server Agent",
            instructions=(
                "You are the Codex MCP server agent."
                "You handle communication between the Voice Assisstant calling you with a task prompt and Codex CLI."
                "Always respond with the Codex CLI output once the task is done."
                "Never try to do any coding task by yourself. Always delegate the task to Codex CLI."
                "By default call Codex with \"approval-policy\": \"never\" and \"sandbox\": \"workspace-write\". "
                "If the voice assistant provides updated session settings, apply those instead."
                ),
            mcp_servers=[],
        )

class CodexCLISession(SQLiteSession):
    """Session class for Codex MCP Agent."""
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id=session_id)
    
    def rename(self, session_id: str) -> None:
        """Update the session identifier while reusing the same underlying storage."""
        self.session_id = session_id

class CodexCLIAgent():
    """Codex Agent to send tasks to Codex via MCP server."""
    def __init__(self) -> None:
        self.server_agent = CodexMCPAgent()
        self.session = CodexCLISession(session_id="codex_agent_session")
        self.settings: Dict[str, Any] = {
            "approval_policy": "never",
            "model": "default",
            "web_search_enabled": False,
        }
    
    async def send_task(self, task_prompt: str) -> Any:
        """Sends the task prompt to Codex via MCP server and returns the result."""
        enable_search = bool(self.settings.get("web_search_enabled", False))
        async with CodexMCPServer(enable_search=enable_search) as mcp_server:
            self.server_agent.mcp_servers = [mcp_server]
            result = await Runner.run(self.server_agent, task_prompt, session=self.session)
            return result

    def update_settings(
        self,
        *,
        approval_policy: Optional[str] = None,
        model: Optional[str] = None,
        web_search_enabled: Optional[bool] = None,
    ) -> None:
        """Record desired Codex session settings for future calls."""
        if approval_policy:
            self.settings["approval_policy"] = approval_policy
        if model:
            self.settings["model"] = model
        if web_search_enabled is not None:
            self.settings["web_search_enabled"] = bool(web_search_enabled)

    def rename_session(self, new_session_id: str) -> bool:
        """Rename the active session if possible."""
        try:
            if hasattr(self.session, "rename"):
                self.session.rename(new_session_id)
            else:
                self.session = CodexCLISession(session_id=new_session_id)
            return True
        except Exception as error:
            logger.exception(f"Failed to rename Codex session to {new_session_id}: {error}")
            return False
