"""Simple GUI to stream microphone audio to the Belya LiveKit agent."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

import tkinter as tk
from tkinter import ttk

try:
    import numpy as np
except ImportError:  # pragma: no cover - environment without numpy
    np = None  # type: ignore

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - environment without sounddevice
    sd = None  # type: ignore

try:
    from livekit import rtc
except ImportError as exc:  # pragma: no cover - LiveKit SDK not installed
    raise RuntimeError(
        "The LiveKit Python SDK is required to use the GUI. Install `livekit-agents`."
    ) from exc

from session_store import SessionStore

logger = logging.getLogger(__name__)


SAMPLE_RATE = 48_000
NUM_CHANNELS = 1


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


@dataclass
class StoredContext:
    room_name: Optional[str] = None
    room_sid: Optional[str] = None
    participant_identity: Optional[str] = None
    participant_sid: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StoredContext":
        return cls(
            room_name=data.get("room_name"),
            room_sid=data.get("room_sid"),
            participant_identity=data.get("participant_identity"),
            participant_sid=data.get("participant_sid"),
            updated_at=data.get("updated_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.room_name:
            payload["room_name"] = self.room_name
        if self.room_sid:
            payload["room_sid"] = self.room_sid
        if self.participant_identity:
            payload["participant_identity"] = self.participant_identity
        if self.participant_sid:
            payload["participant_sid"] = self.participant_sid
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        return payload


class LiveKitStateManager:
    """Wrapper around :class:`SessionStore` for LiveKit metadata."""

    def __init__(self) -> None:
        self._store = SessionStore()

    def load(self) -> StoredContext:
        try:
            state = self._store.get_livekit_state()
            if isinstance(state, dict):
                return StoredContext.from_dict(state)
        except Exception:
            logger.exception("Failed to load stored LiveKit state")
        return StoredContext()

    def save(self, updates: Dict[str, Any]) -> StoredContext:
        existing = self.load()
        merged = existing.to_dict()
        for key, value in updates.items():
            if value:
                merged[key] = value
        merged["updated_at"] = _now_iso()
        try:
            self._store.set_livekit_state(merged)
        except Exception:
            logger.exception("Failed to persist LiveKit state")
        return StoredContext.from_dict(merged)


class LiveKitAudioClient:
    """Minimal LiveKit client that publishes microphone audio."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._room: Optional[rtc.Room] = None
        self._audio_source: Optional[rtc.AudioSource] = None
        self._audio_track: Optional[rtc.LocalAudioTrack] = None

    @property
    def room(self) -> Optional[rtc.Room]:
        return self._room

    @property
    def audio_source(self) -> Optional[rtc.AudioSource]:
        return self._audio_source

    async def connect(self, url: str, token: str) -> None:
        if self._room is not None:
            raise RuntimeError("Already connected to a LiveKit room")

        room = rtc.Room(loop=self._loop)
        try:
            await room.connect(url, token)
            audio_source = rtc.AudioSource(
                sample_rate=SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
                loop=self._loop,
            )
            audio_track = rtc.LocalAudioTrack.create_audio_track(
                "belya-microphone", audio_source
            )
            await room.local_participant.publish_track(audio_track)
        except Exception:
            with contextlib.suppress(Exception):
                await room.disconnect()
            raise

        self._room = room
        self._audio_source = audio_source
        self._audio_track = audio_track

    async def disconnect(self) -> None:
        if self._room is None:
            return
        try:
            await self._room.disconnect()
        finally:
            self._room = None
            self._audio_source = None
            self._audio_track = None

    async def send_audio_frame(self, pcm_data: bytes, samples_per_channel: int) -> None:
        if self._audio_source is None:
            raise RuntimeError("Audio source not ready; connect to a room first")
        frame = rtc.AudioFrame(
            data=pcm_data,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            samples_per_channel=samples_per_channel,
        )
        await self._audio_source.capture_frame(frame)

    def context_info(self) -> Dict[str, Any]:
        if self._room is None:
            return {}
        participant = getattr(self._room, "local_participant", None)
        return {
            "room_name": getattr(self._room, "name", None),
            "room_sid": getattr(self._room, "sid", None),
            "participant_identity": getattr(participant, "identity", None),
            "participant_sid": getattr(participant, "sid", None),
        }


class LiveKitGUI:
    def __init__(self) -> None:
        logging.basicConfig(level=logging.INFO)
        self.root = tk.Tk()
        self.root.title("Belya Voice Assistant – LiveKit Bridge")

        self.state_manager = LiveKitStateManager()

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True
        )
        self._loop_thread.start()
        self.client = LiveKitAudioClient(self._loop)

        self.url_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Disconnected")
        self.context_var = tk.StringVar(value=self._format_context(self.state_manager.load()))

        self._mic_stream: Optional[sd.InputStream] = None  # type: ignore[assignment]
        self._audio_supported = sd is not None and np is not None

        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if not self._audio_supported:
            self._append_log(
                "sounddevice or numpy is not available. Install the optional dependencies to stream audio."
            )
            self.start_button.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Tkinter layout helpers
    # ------------------------------------------------------------------
    def _build_layout(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.grid(row=0, column=0, sticky="nsew")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)

        ttk.Label(container, text="LiveKit URL:").grid(row=0, column=0, sticky="w")
        url_entry = ttk.Entry(container, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0))

        ttk.Label(container, text="Access Token:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        token_entry = ttk.Entry(container, textvariable=self.token_var, show="*")
        token_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))

        self.connect_button = ttk.Button(
            container, text="Connect", command=self._on_connect_clicked
        )
        self.connect_button.grid(row=2, column=1, sticky="ew", pady=(12, 0))

        self.disconnect_button = ttk.Button(
            container,
            text="Disconnect",
            command=self._on_disconnect_clicked,
            state=tk.DISABLED,
        )
        self.disconnect_button.grid(row=2, column=2, sticky="ew", pady=(12, 0), padx=(8, 0))

        self.start_button = ttk.Button(
            container,
            text="Start Microphone",
            command=self._on_start_microphone,
            state=tk.DISABLED,
        )
        self.start_button.grid(row=3, column=1, sticky="ew", pady=(8, 0))

        self.stop_button = ttk.Button(
            container,
            text="Stop Microphone",
            command=self._on_stop_microphone,
            state=tk.DISABLED,
        )
        self.stop_button.grid(row=3, column=2, sticky="ew", pady=(8, 0), padx=(8, 0))

        ttk.Label(container, textvariable=self.status_var).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(12, 0)
        )

        ttk.Separator(container, orient=tk.HORIZONTAL).grid(
            row=5, column=0, columnspan=4, sticky="ew", pady=(12, 12)
        )

        ttk.Label(container, text="Stored LiveKit Context:").grid(
            row=6, column=0, columnspan=4, sticky="w"
        )
        self.context_label = ttk.Label(
            container, textvariable=self.context_var, justify=tk.LEFT, wraplength=420
        )
        self.context_label.grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 12))

        refresh_button = ttk.Button(
            container, text="Refresh Context", command=self._refresh_context
        )
        refresh_button.grid(row=8, column=0, sticky="w")

        self.log_text = tk.Text(container, height=12, wrap="word")
        self.log_text.grid(
            row=9, column=0, columnspan=4, sticky="nsew", pady=(12, 0)
        )
        container.rowconfigure(9, weight=1)

        scrollbar = ttk.Scrollbar(
            container, orient="vertical", command=self.log_text.yview
        )
        scrollbar.grid(row=9, column=4, sticky="nsw", pady=(12, 0))
        self.log_text.configure(yscrollcommand=scrollbar.set, state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Async helpers
    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _schedule_async(
        self,
        coro: Awaitable[Any],
        *,
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _callback(fut: asyncio.Future[Any]) -> None:
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - best effort logging
                logger.exception("Async LiveKit operation failed")
                self.root.after(0, self._handle_async_error, exc, on_error)
            else:
                self.root.after(0, self._handle_async_success, result, on_success)

        future.add_done_callback(_callback)

    def _handle_async_success(
        self, result: Any, callback: Optional[Callable[[Any], None]]
    ) -> None:
        if callback:
            callback(result)

    def _handle_async_error(
        self, exc: Exception, callback: Optional[Callable[[Exception], None]]
    ) -> None:
        message = f"Operation failed: {exc}"
        self._append_log(message)
        self.status_var.set("Error")
        if callback:
            callback(exc)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _on_connect_clicked(self) -> None:
        url = self.url_var.get().strip()
        token = self.token_var.get().strip()
        if not url or not token:
            self._append_log("Provide both the LiveKit URL and an access token.")
            return

        self.status_var.set("Connecting…")
        self._append_log("Connecting to LiveKit room…")
        self.connect_button.configure(state=tk.DISABLED)

        self._schedule_async(
            self.client.connect(url, token),
            on_success=self._on_connected,
            on_error=self._on_connect_error,
        )

    def _on_connected(self, _result: Any) -> None:
        self._append_log("Connected to LiveKit. Microphone streaming is available.")
        self.status_var.set("Connected")
        self.disconnect_button.configure(state=tk.NORMAL)
        if self._audio_supported:
            self.start_button.configure(state=tk.NORMAL)
        context = self.client.context_info()
        stored = self.state_manager.save(context)
        self.context_var.set(self._format_context(stored))

    def _on_connect_error(self, exc: Exception) -> None:
        self._append_log(f"Failed to connect: {exc}")
        self.status_var.set("Disconnected")
        self.connect_button.configure(state=tk.NORMAL)

    def _on_disconnect_clicked(self) -> None:
        if self.client.room is None:
            self._append_log("No active LiveKit connection.")
            return
        self._append_log("Disconnecting from LiveKit…")
        self.status_var.set("Disconnecting…")
        self._on_stop_microphone()
        self._schedule_async(
            self.client.disconnect(),
            on_success=self._on_disconnected,
            on_error=self._on_disconnect_error,
        )

    def _on_disconnected(self, _result: Any) -> None:
        self._append_log("Disconnected from LiveKit room.")
        self.status_var.set("Disconnected")
        self.connect_button.configure(state=tk.NORMAL)
        self.disconnect_button.configure(state=tk.DISABLED)
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.DISABLED)

    def _on_disconnect_error(self, exc: Exception) -> None:
        self._append_log(f"Error while disconnecting: {exc}")
        self.status_var.set("Disconnected")
        self.connect_button.configure(state=tk.NORMAL)
        self.disconnect_button.configure(state=tk.DISABLED)
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.DISABLED)

    def _on_start_microphone(self) -> None:
        if not self._audio_supported:
            self._append_log("Audio capture is not available on this system.")
            return
        if self._mic_stream is not None:
            return
        if self.client.audio_source is None:
            self._append_log("Connect to the LiveKit room before streaming audio.")
            return

        try:
            self._mic_stream = sd.InputStream(  # type: ignore[call-arg]
                samplerate=SAMPLE_RATE,
                channels=NUM_CHANNELS,
                dtype="float32",
                callback=self._on_audio_chunk,
            )
            self._mic_stream.start()
        except Exception as exc:  # pragma: no cover - depends on host audio stack
            self._append_log(f"Unable to start microphone: {exc}")
            self.status_var.set("Microphone error")
            self._mic_stream = None
            return

        self._append_log("Microphone streaming started.")
        self.status_var.set("Streaming audio")
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)

    def _on_stop_microphone(self) -> None:
        if self._mic_stream is None:
            return
        try:
            self._mic_stream.stop()
            self._mic_stream.close()
        except Exception:  # pragma: no cover - depends on host audio stack
            logger.exception("Failed to stop microphone stream")
        finally:
            self._mic_stream = None
        self._append_log("Microphone streaming stopped.")
        if self.client.room is not None and self._audio_supported:
            self.start_button.configure(state=tk.NORMAL)
        else:
            self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.DISABLED)
        if self.client.room is not None:
            self.status_var.set("Connected")
        else:
            self.status_var.set("Disconnected")

    def _refresh_context(self) -> None:
        context = self.state_manager.load()
        self.context_var.set(self._format_context(context))
        self._append_log("Loaded stored LiveKit context.")

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------
    def _on_audio_chunk(self, indata, frames, time_info, status) -> None:
        if not self._audio_supported or self.client.audio_source is None:
            return
        if status:
            self._append_log(f"Microphone status warning: {status}")

        pcm = np.clip(indata[:, :NUM_CHANNELS], -1.0, 1.0)  # type: ignore[index]
        pcm_int16 = (pcm * 32767).astype(np.int16)
        pcm_bytes = pcm_int16.tobytes()

        future = asyncio.run_coroutine_threadsafe(
            self.client.send_audio_frame(pcm_bytes, frames),
            self._loop,
        )

        def _on_audio_error(fut: asyncio.Future) -> None:
            with contextlib.suppress(Exception):
                fut.result()

        future.add_done_callback(_on_audio_error)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _format_context(self, context: StoredContext) -> str:
        if not any(vars(context).values()):
            return "No stored LiveKit context yet. Connect once to populate."
        parts = []
        if context.room_name:
            parts.append(f"Room: {context.room_name}")
        if context.room_sid:
            parts.append(f"Room SID: {context.room_sid}")
        if context.participant_identity:
            parts.append(f"Participant: {context.participant_identity}")
        if context.participant_sid:
            parts.append(f"Participant SID: {context.participant_sid}")
        if context.updated_at:
            parts.append(f"Last updated: {context.updated_at}")
        return "\n".join(parts)

    def _on_close(self) -> None:
        self._on_stop_microphone()

        def _finalize() -> None:
            self.root.destroy()
            self._loop.call_soon_threadsafe(self._loop.stop)

        def _shutdown_complete(_result: Any) -> None:
            _finalize()

        if self.client.room is not None:
            self._schedule_async(
                self.client.disconnect(),
                on_success=_shutdown_complete,
                on_error=lambda _exc: _finalize(),
            )
        else:
            _finalize()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    LiveKitGUI().run()
