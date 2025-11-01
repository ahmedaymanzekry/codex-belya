import logging

from dotenv import load_dotenv
from livekit.agents import (
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RoomInputOptions,
    RoomOutputOptions,
    WorkerOptions,
    cli,
    metrics,
)
from livekit.plugins import noise_cancellation, openai, silero

from belya_agents.head_belya import HeadBelyaAgent

logger = logging.getLogger("basic-agent")

load_dotenv()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=openai.STT(),
        llm=openai.LLM(),
        tts=openai.TTS(instructions="Use a friendly and professional tone of voice. Be cheerful and encouraging. Sound excited to help the user."),
        preemptive_generation=True,
        resume_false_interruption=True,
        false_interruption_timeout=1.0,
    )

    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info("Usage: %s", summary)

    ctx.add_shutdown_callback(log_usage)

    supervisor = HeadBelyaAgent()
    stored_livekit_state = supervisor.get_livekit_state()

    stored_room_sid = stored_livekit_state.get("room_sid") or stored_livekit_state.get("room_id")
    if stored_room_sid:
        ctx.log_context_fields["stored_room_sid"] = stored_room_sid

    room_input_options = RoomInputOptions(
        noise_cancellation=noise_cancellation.BVC(),
    )
    room_output_options = RoomOutputOptions(transcription_enabled=True)

    participant_hint = (
        stored_livekit_state.get("participant_identity")
        or stored_livekit_state.get("participant_sid")
        or stored_livekit_state.get("participant_id")
    )

    if participant_hint:
        for attr in ("identity", "participant_identity"):
            if hasattr(room_input_options, attr):
                setattr(room_input_options, attr, participant_hint)
                break
        for attr in ("identity", "participant_identity"):
            if hasattr(room_output_options, attr):
                setattr(room_output_options, attr, participant_hint)
                break

    try:
        await session.start(
            agent=supervisor,
            room=ctx.room,
            room_input_options=room_input_options,
            room_output_options=room_output_options,
        )
    finally:
        room_obj = getattr(ctx, "room", None)
        room_info = {
            "room_id": getattr(room_obj, "sid", None) or getattr(room_obj, "name", None),
            "room_sid": getattr(room_obj, "sid", None),
            "room_name": getattr(room_obj, "name", None),
        }

        agent_participant = getattr(session, "agent_participant", None)
        if agent_participant is None:
            agent_participant = getattr(session, "participant", None)

        participant_info = {
            "participant_id": getattr(agent_participant, "sid", None) or getattr(agent_participant, "identity", None),
            "participant_sid": getattr(agent_participant, "sid", None),
            "participant_identity": getattr(agent_participant, "identity", None),
        }

        supervisor.record_livekit_context(room_info, participant_info)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
