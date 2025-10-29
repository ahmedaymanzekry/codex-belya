import asyncio
import logging
from typing import Any, Dict

from livekit.agents import RunContext, function_tool


logger = logging.getLogger(__name__)


class CodexTaskToolsMixin:
    """Mixin providing Codex task execution tools."""

    @function_tool
    async def send_task_to_Codex(self, task_prompt: str, run_ctx: RunContext) -> str | None:
        """Send a task prompt to Codex CLI and relay the response."""
        try:
            logger.info("Sending the following task prompt to Codex CLI %s.", task_prompt)

            run_ctx.disallow_interruptions()

            wait_for_result = asyncio.ensure_future(self._a_long_running_task(task_prompt))
            try:
                await run_ctx.speech_handle.wait_if_not_interrupted([wait_for_result])
            except Exception:
                wait_for_result.cancel()
                raise

            if run_ctx.speech_handle.interrupted:
                logger.info("Interrupted receiving reply from Codex task with prompt %s", task_prompt)
                wait_for_result.cancel()
                return None

            result_bundle = wait_for_result.result()
            output_text = result_bundle.get("output") if isinstance(result_bundle, dict) else result_bundle
            raw_result = result_bundle.get("raw_result") if isinstance(result_bundle, dict) else None
            if not isinstance(output_text, str):
                output_text = self._extract_final_output(raw_result, task_prompt)
            logger.info("Done receiving Codex reply for task prompt %s, result: %s", task_prompt, output_text)
            warning_message = self._post_process_codex_activity(
                task_prompt,
                output_text,
                raw_result,
                entry_type="task",
            )
            if warning_message:
                output_text = f"{output_text}\n\n{warning_message}"
            return output_text
        except Exception as error:
            return self._handle_tool_error("sending the task to Codex", error)

    async def _a_long_running_task(self, task_prompt: str) -> Dict[str, Any]:
        """Run the Codex task asynchronously and capture the result."""
        results = await self.CodexAgent.send_task(task_prompt)
        output_text = self._extract_final_output(results, task_prompt)
        logger.info("Finished long running Codex task for prompt %s.", task_prompt)
        return {
            "output": output_text,
            "raw_result": results,
        }
