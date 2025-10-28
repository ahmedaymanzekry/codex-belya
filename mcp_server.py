from agents import Agent, Runner, SQLiteSession
from agents.mcp import MCPServerStdio

class CodexMCPServer(MCPServerStdio):
    def __init__(self) -> None:
        super().__init__(
            name="Codex MCP Server",
            params={
                "command": "npx",
                "args": ["-y", "codex", "mcp-server"],
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
                "Always call codex with \"approval-policy\": \"never\" and \"sandbox\": \"workspace-write\"."
                ),
            mcp_servers=[],
        )

class CodexCLISession(SQLiteSession):
    """Session class for Codex MCP Agent."""
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id=session_id)

class CodexCLIAgent():
    """Codex Agent to send tasks to Codex via MCP server."""
    def __init__(self) -> None:
        self.server_agent = CodexMCPAgent()
        self.session = CodexCLISession(session_id="codex_agent_session")
    
    async def send_task(self, task_prompt: str) -> str:
        """Sends the task prompt to Codex via MCP server and returns the result."""
        async with CodexMCPServer() as mcp_server:
            self.server_agent.mcp_servers = [mcp_server]
            result = await Runner.run(self.server_agent, task_prompt, session=self.session)
            return result.final_output