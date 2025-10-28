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

from mcp_server import CodexCLIAgent, CodexCLISession

from git import Repo

import os

logger = logging.getLogger("basic-agent")

load_dotenv()

class VoiceAssistantAgent(Agent):
    def __init__(self) -> None:
        self.CodexAgent = CodexCLIAgent()
        self.sessions_ids_used = []
        super().__init__(
            instructions="Your name is Belya. You are a helpful voice assistant for Codex users. Your interface with users will be Voice.\
                You help users in the following:\
                1. collecting all the coding tasks they need from Codex to work on. Make sure you have all the needed work before sending it to Codex CLI.\
                2. creating a single prompt for all the coding requests from the user to communicate to Codex.\
                3. Get the code response, once Codex finish the task.\
                4. reading out the code response to the user via voice; focusing on the task actions done and the list of tests communicated back from Codex. Do not read the diffs.\
                Ask the user if they have any more tasks to send to Codex, and repeat the process until the user is done.\
                After their first task, ask them if they want to continue with the task or start a new one. use the 'start_a_new_session' function if they chose to start a new codex task. \
                Any new session should have a different id than previous sessions.\
                review the prompt with the user before sending it to the 'send_task_to_Codex' function. \
                Always use the `send_task_to_Codex` tool to send any coding task to Codex CLI.\
                Make sure you notify the user of the current branch before they start a new session/task. use the 'check_current_branch' to get the current branch.\
                Ask the user if he wants to create a new branch and if the user approve, start a new branch in the repo before sending new tasks to Codex CLI.\
                Do not change the branch mid-session.\
                Ask the user if they have a preference for the branch name, and verify the branch name. use the 'create branch' tool.\
                Never try to do any coding task by yourself. Do not ask the user to provide any code.\
                Always wait for the Codex response before reading it out to the user.\
                Be polite and professional. Sound excited to help the user.",
    )
    
    def _handle_tool_error(self, action: str, error: Exception) -> str:
        logger.exception(f"Error while {action}: {error}")
        return f"I ran into an error while {action}: {error}"
    
    async def on_enter(self):
        # when the agent is added to the session, it'll generate a reply
        # according to its instructions
        self.session.generate_reply(instructions="greet the user and introduce yourself as Belya, a voice assistant for Codex users.")

    @function_tool
    async def check_current_branch(self) -> str:
        """Called when user wants to know the current branch in the repo."""
        try:
            repo_path = os.getcwd()  # assuming the current working directory is the repo path
            repo = Repo(repo_path)
            current_branch = repo.active_branch.name
            logger.info(f"Current branch in repo at {repo_path} is {current_branch}.")
            return f"Current branch in the repo is {current_branch}."
        except Exception as error:
            return self._handle_tool_error("checking the current branch", error)
    
    @function_tool
    async def create_branch(self, branch_name: str) -> str:
        """Called when user wants to create a new branch in the repo for Codex to work on.
        Args:
            branch_name: The name of the new branch to be created.
        """
        try:
            repo_path = os.getcwd()  # assuming the current working directory is the repo path
            repo = Repo(repo_path)
            if branch_name in [head.name for head in repo.heads]:
                logger.info(f"Branch {branch_name} already exists in repo at {repo_path}.")
                return f"The branch {branch_name} already exists. Please pick a different name or switch to it."
            # create new branch
            repo.git.checkout("HEAD", b=branch_name)
            logger.info(f"Created and checked out new branch {branch_name} in repo at {repo_path}.")
            return f"Created and checked out new branch {branch_name} in the repo."
        except Exception as error:
            return self._handle_tool_error("creating a new branch", error)

    @function_tool
    async def commit_changes(self, commit_message: str) -> str:
        """Called when user wants to commit all current changes with a message.
        Args:
            commit_message: The commit message to use.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            if not repo.is_dirty(untracked_files=True):
                logger.info(f"No changes to commit in repo at {repo_path}.")
                return "There are no changes to commit."

            repo.git.add(all=True)
            commit = repo.index.commit(commit_message)
            logger.info(
                f"Committed changes in repo at {repo_path} with message '{commit_message}'. Commit id: {commit.hexsha}."
            )
            return f"Committed changes with message: {commit_message}."
        except Exception as error:
            return self._handle_tool_error("committing changes", error)

    @function_tool
    async def pull_updates(self, remote_name: str = "origin", branch_name: str | None = None) -> str:
        """Called when user wants to pull the latest updates from the remote branch.
        Args:
            remote_name: The name of the remote to pull from. Defaults to 'origin'.
            branch_name: The name of the branch to pull. Defaults to the current branch.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            active_branch = repo.active_branch.name
            branch_to_pull = branch_name or active_branch
            remote = repo.remote(remote_name)
            pull_infos = remote.pull(branch_to_pull)
            summaries = ", ".join(
                info.summary for info in pull_infos if hasattr(info, "summary") and info.summary
            )
            logger.info(
                f"Pulled updates from {remote_name}/{branch_to_pull} in repo at {repo_path}. Summaries: {summaries}"
            )
            if not summaries:
                summaries = "Pull completed with no additional details."
            return f"Pulled latest updates from {remote_name}/{branch_to_pull}. {summaries}"
        except Exception as error:
            return self._handle_tool_error("pulling updates", error)

    @function_tool
    async def fetch_updates(self, remote_name: str = "origin") -> str:
        """Called when user wants to fetch updates from the remote without merging."""
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            remote = repo.remote(remote_name)
            fetch_infos = remote.fetch()
            summaries = ", ".join(
                info.summary for info in fetch_infos if hasattr(info, "summary") and info.summary
            )
            logger.info(
                f"Fetched updates from {remote_name} in repo at {repo_path}. Summaries: {summaries}"
            )
            if not summaries:
                summaries = "Fetch completed with no additional details."
            return f"Fetched updates from {remote_name}. {summaries}"
        except Exception as error:
            return self._handle_tool_error("fetching updates", error)

    @function_tool
    async def list_branches(self) -> str:
        """Called when user wants to list all local branches."""
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            branches = [head.name for head in repo.heads]
            current_branch = repo.active_branch.name

            if not branches:
                logger.info(f"No branches found in repo at {repo_path}.")
                return "No branches found in the repository."

            formatted_branches = [
                f"{name} (current)" if name == current_branch else name for name in branches
            ]
            branch_list = ", ".join(formatted_branches)
            logger.info(f"Listed branches in repo at {repo_path}: {branch_list}")
            return f"The local branches are: {branch_list}."
        except Exception as error:
            return self._handle_tool_error("listing branches", error)

    @function_tool
    async def delete_branch(self, branch_name: str, force: bool = False) -> str:
        """Called when user wants to delete a local branch.
        Args:
            branch_name: The name of the branch to delete.
            force: Whether to force delete the branch even if it is not fully merged.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            current_branch = repo.active_branch.name
            if branch_name == current_branch:
                logger.info(
                    f"Attempted to delete current branch {branch_name} in repo at {repo_path}."
                )
                return "Cannot delete the branch you are currently on. Please switch to another branch first."

            if branch_name not in [head.name for head in repo.heads]:
                logger.info(f"Attempted to delete non-existent branch {branch_name} in repo at {repo_path}.")
                return f"The branch {branch_name} does not exist."

            flag = "-D" if force else "-d"
            repo.git.branch(flag, branch_name)
            logger.info(
                f"Deleted branch {branch_name} in repo at {repo_path} with force={force}."
            )
            return f"Deleted branch {branch_name}."
        except Exception as error:
            return self._handle_tool_error("deleting the branch", error)

    @function_tool
    async def push_branch(
        self, remote_name: str = "origin", branch_name: str | None = None, set_upstream: bool | None = None
    ) -> str:
        """Called when user wants to push the current or specified branch to a remote.
        Args:
            remote_name: The remote to push to. Defaults to 'origin'.
            branch_name: The branch to push. Defaults to the current branch.
            set_upstream: Force setting upstream; if None it is auto-detected.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            remote = repo.remote(remote_name)

            active_branch = repo.active_branch
            branch_to_push = branch_name or active_branch.name

            if branch_to_push not in [head.name for head in repo.heads]:
                logger.info(
                    f"Attempted to push non-existent branch {branch_to_push} in repo at {repo_path}."
                )
                return f"The branch {branch_to_push} does not exist locally."

            head_ref = next(head for head in repo.heads if head.name == branch_to_push)
            tracking_branch = head_ref.tracking_branch()

            should_set_upstream = set_upstream if set_upstream is not None else tracking_branch is None

            if should_set_upstream:
                push_result = remote.push(f"{branch_to_push}:{branch_to_push}", set_upstream=True)
            else:
                push_result = remote.push(branch_to_push)

            summaries = ", ".join(
                info.summary for info in push_result if hasattr(info, "summary") and info.summary
            )
            logger.info(
                f"Pushed branch {branch_to_push} to {remote_name} from repo at {repo_path}. "
                f"Set upstream: {should_set_upstream}. Summaries: {summaries}"
            )
            if not summaries:
                summaries = "Push completed with no additional details."

            upstream_msg = (
                "Upstream branch configured."
                if should_set_upstream
                else "Used existing upstream."
            )
            return f"Pushed {branch_to_push} to {remote_name}. {upstream_msg} {summaries}"
        except Exception as error:
            return self._handle_tool_error("pushing the branch", error)

    @function_tool
    async def switch_branch(self, branch_name: str) -> str:
        """Called when user wants to switch to an existing branch.
        Args:
            branch_name: The name of the branch to switch to.
        """
        try:
            repo_path = os.getcwd()
            repo = Repo(repo_path)
            if branch_name not in [head.name for head in repo.heads]:
                logger.info(
                    f"Attempted to switch to non-existent branch {branch_name} in repo at {repo_path}."
                )
                return f"The branch {branch_name} does not exist."

            repo.git.checkout(branch_name)
            logger.info(f"Switched to branch {branch_name} in repo at {repo_path}.")
            return f"Switched to branch {branch_name}."
        except Exception as error:
            return self._handle_tool_error("switching branches", error)

    @function_tool
    async def start_a_new_session(self, session_id: str) -> str:
        """Called when user wants to start a new Codex task session."""
        try:
            self.sessions_ids_used.append(self.CodexAgent.session.session_id)
            # reset the Codex agent session with the new session id
            
            if session_id in self.sessions_ids_used:
                logger.info(f"Session id {session_id} has been used before. Asking user for a different session id.")
                return f"The session id {session_id} has been used before. Please provide a different session id for the new Codex task session."
            
            self.CodexAgent.session = CodexCLISession(session_id=session_id)
            logger.info(f"Started a new Codex agent session.")
            return "Started a new Codex task session. Please provide the new coding task you want Codex to work on."
        except Exception as error:
            return self._handle_tool_error("starting a new Codex session", error)
    
    @function_tool
    async def send_task_to_Codex(self, task_prompt: str, run_ctx: RunContext) -> str | None:
        """Called when user asks to send a task prompt to Codex.
        Args:
            task_prompt: The prompt text describing the task to be sent to Codex CLI.
            run_ctx: The run context for this function call.
        """
        try:
            logger.info(f"Sending the following task prompt to Codex CLI {task_prompt}.")

            # wait for the task to finish or the agent speech to be interrupted
            # alternatively, you can disallow interruptions for this function call with
            run_ctx.disallow_interruptions()

            wait_for_result = asyncio.ensure_future(self._a_long_running_task(task_prompt))
            try:
                await run_ctx.speech_handle.wait_if_not_interrupted([wait_for_result])
            except Exception:
                wait_for_result.cancel()
                raise

            if run_ctx.speech_handle.interrupted:
                logger.info(f"Interrupted receiving reply from Codex task with prompt {task_prompt}")
                # return None to skip the tool reply
                wait_for_result.cancel()
                return None

            output = wait_for_result.result()
            logger.info(f"Done receiving Codex reply for the task with prompt {task_prompt}, result: {output}")
            return output
        except Exception as error:
            return self._handle_tool_error("sending the task to Codex", error)

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
