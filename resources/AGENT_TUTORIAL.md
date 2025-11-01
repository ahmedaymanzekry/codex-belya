# Head-Belya Agent & Tool Tutorial

This walkthrough shows you how to extend Head-Belya with a new specialist agent and its function tools. It assumes you are starting with a clean clone of this repository and want to add a focused capability that the voice supervisor can delegate to.

## 0. Prerequisites

- Python 3.10+ with dependencies installed via `pip install -r requirements.txt`.
- Working knowledge of the `livekit.agents` SDK (`Agent`, `RunContext`, and `@function_tool`).
- Comfort editing files under `belya_agents/` and `tools/`.
- A well-defined responsibility for the new capability (e.g., “summarize documentation”, “trigger deployments”).

## 1. Understand the Existing Layout

- `belya_agents/head_belya.py` – The supervisor that owns the conversation and delegates to specialists.
- `belya_agents/codex_belya.py` and `belya_agents/git_belya.py` – Reference implementations for small, single-purpose agents.
- `belya_agents/shared.py` – Home for mixins that provide logging, error helpers, and consistent behaviours.
- `tools/` – Mixins that bundle groups of function tools. Each coroutine decorated with `@function_tool` becomes callable through the LiveKit runtime.

Skim these files so you can mirror their structure when wiring in your new agent.

## 2. Plan Your Specialist Agent

- Give the agent **one job** so the supervisor can decide when to call it. Examples: `DocsBelyaAgent` for documentation lookup, `DeployBelyaAgent` for CI triggers, `MetricsBelyaAgent` for project analytics.
- Decide what inputs the tools require and what format they should return.
- Write a short instruction block that tells the language model how to behave when the agent is active.

Document the answers in your issue or design note; they feed directly into the code you are about to write.

## 3. Scaffold the Agent Class

Create a new file inside `belya_agents/`. Inherit from `AgentUtilitiesMixin` (for shared logging/error handling) and optionally from one or more tool mixins. Call `super().__init__` with the instruction string so LiveKit can brief the model.

```python
# belya_agents/docs_belya.py
from livekit.agents import Agent

from .shared import AgentUtilitiesMixin
from tools.docs_tools import DocsToolsMixin


class DocsBelyaAgent(AgentUtilitiesMixin, DocsToolsMixin, Agent):
    """Documentation specialist invoked by Head-Belya."""

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are docs-belya. Resolve documentation questions forwarded by "
                "head-belya. Stick to authoritative docs sources."
            )
        )
```

Tips:

- Keep the inheritance order: mixins first, then `Agent` last.
- The instructions should recap the agent’s scope and boundaries in plain language.
- If your agent needs constructor arguments (API keys, config paths), thread them through `HeadBelyaAgent` when you instantiate the specialist.

## 4. Implement or Import Function Tools

Function tools expose concrete capabilities. You can define them directly on the agent class or (recommended) on a mixin under `tools/` so they can be reused elsewhere.

```python
# tools/docs_tools.py
from livekit.agents import function_tool


class DocsToolsMixin:
    @function_tool
    async def lookup_document(self, title: str) -> str:
        """Return a short summary of the requested documentation page."""

        try:
            content = await self._fetch_doc(title)
        except FileNotFoundError as exc:  # noqa: PERF203 - keep explicit for clarity
            return self._handle_tool_error(str(exc))

        return self._summarize_doc(content)

    async def _fetch_doc(self, title: str) -> str:
        ...

    def _summarize_doc(self, content: str) -> str:
        ...
```

Guidelines:

- Use `@function_tool` on async methods you want exposed externally.
- Return user-friendly strings; reuse `_handle_tool_error` from `AgentUtilitiesMixin` to keep responses polite when exceptions occur.
- Keep helper methods private (no decorator) so they remain internal utilities.

## 5. Register the Agent with the Supervisor

Edit `belya_agents/head_belya.py` and make the following additions:

1. **Import and instantiate the specialist**
   ```python
   from .docs_belya import DocsBelyaAgent


   class HeadBelyaAgent(...):
       def __init__(self) -> None:
           ...
           self.docs_agent = DocsBelyaAgent()
           ...
   ```

2. **Expose the tools to LiveKit through delegators**
   - For each tool, create a wrapper decorated with `@function_tool` that forwards the call to the specialist.
   - Match the signature of the specialist’s tool and pass `run_ctx` through untouched.

   ```python
   from livekit.agents import RunContext, function_tool


   class HeadBelyaAgent(...):
       ...

       @function_tool
       async def lookup_document(self, title: str, run_ctx: RunContext) -> str:
           return await self.docs_agent.lookup_document(title=title, run_ctx=run_ctx)
   ```

3. **Update the supervisor’s instructions (optional but helpful)**
   Add a sentence describing the new capability so the model knows it exists: “Use docs-belya for documentation lookups.”

4. **(Optional) Route by trigger phrases**
   If you rely on heuristics to decide which agent should respond, update the routing logic in `HeadBelyaAgent` accordingly.

## 6. Validate the Integration

- Run the voice loop locally (for example, `python main.py start`) and ask Belya to invoke the new capability. Watch the logs under the `belya-agents` logger to confirm the correct agent handled the task.
- Exercise failure paths (invalid parameters, missing resources). Verify graceful error messages via `_handle_tool_error`.
- If the tool logic is complex, add unit tests under `tests/` that cover success and failure cases. Tests can import the mixin directly without spinning up LiveKit.

## 7. Document and Share

- Mention the new agent and its trigger phrases in `README.md` or project docs so teammates discover it quickly.
- If the agent introduces new configuration (API keys, datasets), add entries to `.env.example` or the relevant setup instructions.
- Consider adding monitoring or metrics if the tools hit external services.

With these steps you can confidently extend Head-Belya’s multi-agent ecosystem. Each new specialist should be small, focused, and pluggable; making it easy for the supervisor to orchestrate complex workflows without sacrificing clarity.
