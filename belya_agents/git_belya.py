from livekit.agents import Agent

from .shared import AgentUtilitiesMixin
from tools.git_tools import GitFunctionToolsMixin


class GitBelyaAgent(AgentUtilitiesMixin, GitFunctionToolsMixin, Agent):
    """Git specialist agent. Owns every git_* function tool."""

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are git-belya. Execute git-focused tools delegated by head-belya. "
                "Stay within git operations and refuse other work."
            )
        )
