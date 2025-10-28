Belya is a traditional Egyptian term used for an assistant working at an auto repair shop. He helps the master mechanic and organize the shop for him.
That's what Codex-Belya is. It serves as an AI voice assistant for Codex; helping you "talk" to your code (which all of us do when we are in the zone) and assign tasks to Codex, while focusing on the nitty gritty review and enhancement.
It let's you organize and communicate your thoughts to assign Codex on it, while you go and focus on formulating the next set of goals.

## LiveKit desktop bridge

Run `python gui.py` to launch a small Tkinter-based control panel for the Belya assistant. The window now pulls its connection
details from environment variables (or a `.env` file) and connects automatically when everything is configured:

* `LIVEKIT_URL` – the websocket URL of your deployment.
* `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` – used to mint short-lived participant tokens.
* `LIVEKIT_ROOM` – optional; if omitted the GUI falls back to the last room stored in `session_store`.
* `LIVEKIT_PARTICIPANT_ID` – optional custom identity; a random value is generated when missing.

With these values in place the GUI mints a JWT on startup, connects to the room, and enables microphone streaming without
requiring any manual input. You can still click **Connect** to retry after a disconnect, and the window surfaces the last
room/participant metadata stored in `session_store`. Install the `livekit-agents`, `numpy`, and `sounddevice` packages for full
functionality.
