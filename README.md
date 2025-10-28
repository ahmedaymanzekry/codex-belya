Belya is a traditional Egyptian term used for an assistant working at an auto repair shop. He helps the master mechanic and organize the shop for him.
That's what Codex-Belya is. It serves as an AI voice assistant for Codex; helping you "talk" to your code (which all of us do when we are in the zone) and assign tasks to Codex, while focusing on the nitty gritty review and enhancement.
It let's you organize and communicate your thoughts to assign Codex on it, while you go and focus on formulating the next set of goals.

## LiveKit desktop bridge

Run `python gui.py` to launch a small Tkinter-based control panel for the Belya assistant. The window lets you:

* enter a LiveKit URL and access token,
* connect or disconnect from the room,
* and stream microphone audio into the room once connected.

The GUI also surfaces the last room and participant identity stored in `session_store` so you can reconnect quickly. It requires the `livekit-agents`, `numpy`, and `sounddevice` packages for full functionality.
