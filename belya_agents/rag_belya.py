from livekit.agents import Agent

from .shared import AgentUtilitiesMixin
from tools.rag_tools import RAGFunctionToolsMixin


class RAGBelyaAgent(AgentUtilitiesMixin, RAGFunctionToolsMixin, Agent):
    """Repository research specialist using a LangChain-powered retrieval workflow."""

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are RAG-belya. Handle repository research questions forwarded by head-belya. "
                "Use the LangChain-backed retrieval tools to inspect the local codebase, extract the most relevant "
                "snippets, and respond with concise findings. Do not modify files or execute git operations; "
                "focus solely on gathering and summarizing information."
            ),
        )

