import asyncio
import logging
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RoomInputOptions,
    RoomOutputOptions,
    RunContext,
    WorkerOptions,
    cli,
    metrics,
    function_tool,
)
# from livekit.plugins.turn_detector.multilingual import MultilingualModel
from livekit.plugins import openai, silero
from livekit.plugins import noise_cancellation

from mcp_server import CodexCLIAgent

from git import Repo

import os

logger = logging.getLogger("basic-agent")

load_dotenv()

class VoiceAssistantAgent(Agent):
    def __init__(self) -> None:
        self.CodexAgent = CodexCLIAgent()
        super().__init__(
            instructions="Your name is Belya. You are a helpful voice assistant for Codex users. Your interface with users will be Voice.\
                You help users in the following:\
                1. collecting all the coding tasks they need from Codex to work on. Make sure you have all the needed work before sending it to Codex CLI.\
                2. creating a single prompt for all the coding requests from the user to communicate to Codex.\
                3. Get the code response, once Codex finish the task.\
                4. reading out the code response to the user via voice; focusing on the task actions done and the list of tests communicated back from Codex. Do not read the diffs.\
                Ask the user if they have any more tasks to send to Codex, and repeat the process until the user is done.\
                review the prompt with the user before sending it to the 'send_task_to_Codex' function. \
                Always use the `send_task_to_Codex` tool to send any coding task to Codex CLI.\
                Make sure you start a new branch in the repo before sending any tasks to Codex CLI.\
                Ask the user if they have a preference for the branch name, and verify the branch name. use the 'create branch' tool.\
                Never try to do any coding task by yourself. Do not ask the user to provide any code.\
                Always wait for the Codex response before reading it out to the user.\
                Be polite and professional. Sound excited to help the user.",
    )
    
    async def on_enter(self):
        # when the agent is added to the session, it'll generate a reply
        # according to its instructions
        self.session.generate_reply(instructions="greet the user and introduce yourself as Belya, a voice assistant for Codex users.")

    @function_tool
    async def create_branch(self, branch_name: str) -> str:
        """Called when user wants to create a new branch in the repo for Codex to work on.
        Args:
            branch_name: The name of the new branch to be created.
        """
        repo_path = os.getcwd()  # assuming the current working directory is the repo path
        repo = Repo(repo_path)
        # create new branch
        repo.git.branch(branch_name)
        logger.info(f"Created and checked out new branch {branch_name} in repo at {repo_path}.")
        return f"Created and checked out new branch {branch_name} in the repo."

    @function_tool
    async def send_task_to_Codex(self, task_prompt: str, run_ctx: RunContext) -> str | None:
        """Called when user asks to send a task prompt to Codex.
        Args:
            task_prompt: The prompt text describing the task to be sent to Codex CLI.
            run_ctx: The run context for this function call.
        """
        logger.info(f"Sending the following task prompt to Codex CLI {task_prompt}.")

        # wait for the task to finish or the agent speech to be interrupted
        # alternatively, you can disallow interruptions for this function call with
        run_ctx.disallow_interruptions()

        wait_for_result = asyncio.ensure_future(self._a_long_running_task(task_prompt))
        await run_ctx.speech_handle.wait_if_not_interrupted([wait_for_result])

        if run_ctx.speech_handle.interrupted:
            logger.info(f"Interrupted receiving reply from Codex task with prompt {task_prompt}")
            # return None to skip the tool reply
            wait_for_result.cancel()
            return None

        output = wait_for_result.result()
        logger.info(f"Done receiving Codex reply for the task with prompt {task_prompt}, result: {output}")
        return output

    async def _a_long_running_task(self, task_prompt: str) -> str:
        """Simulate a long running task."""
        results = await self.CodexAgent.send_task(task_prompt)
        logger.info(f"Finished long running Codex task for prompt {task_prompt}.")
        return f"I got some results for Codex task working on the prompt {task_prompt}. Here are the details: {results}"
    
def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    session = AgentSession(
        # turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        # any combination of STT, LLM, TTS, or realtime API can be used
        stt=openai.STT(),
        llm=openai.LLM(),
        tts=openai.TTS(instructions="Use a friendly and professional tone of voice. Be cheerful and encouraging. Sound excited to help the user."),
        # allow the LLM to generate a response while waiting for the end of turn
        # See more at https://docs.livekit.io/agents/build/audio/#preemptive-generation
        preemptive_generation=True,
        # sometimes background noise could interrupt the agent session, these are considered false positive interruptions
        # when it's detected, you may resume the agent's speech
        resume_false_interruption=True,
        false_interruption_timeout=1.0,
    )

    # log metrics as they are emitted, and total usage after session is over
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info(f"Usage: {summary}")

    # shutdown callbacks are triggered when the session is over
    ctx.add_shutdown_callback(log_usage)

    await session.start(
        agent=VoiceAssistantAgent(),
        room=ctx.room,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
        ),
        room_output_options=RoomOutputOptions(transcription_enabled=True),
    )

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))