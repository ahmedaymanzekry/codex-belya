Belya is a traditional Egyptian term used for an assistant working at an auto repair shop. He helps the master mechanic and organize the shop for him.
That's what Codex-Belya is. It serves as an AI voice assistant for Codex; helping you "talk" to your code (which all of us do when we are in the zone) and assign tasks to Codex, while focusing on the nitty gritty review and enhancement.
It let's you organize and communicate your thoughts to assign Codex on it, while you go and focus on formulating the next set of goals.

## Prerequisites

- Python 3.12+
- Node.js 18+ and npm
- A reachable [LiveKit](https://livekit.io/) deployment with API key/secret and a room where the voice agent will operate

## Environment configuration

Create a `.env` file in the project root (or use your preferred secrets manager) with the following variables:

```
LIVEKIT_URL=https://your-livekit-host
LIVEKIT_API_KEY=your-api-key
LIVEKIT_API_SECRET=your-api-secret
# Optional overrides
WEB_APP_PORT=3000
LIVEKIT_TOKEN_TTL=3600
```

The Python agent shares LiveKit room and participant metadata with the embedded web server, which then mints browser tokens on demand. The optional variables let you customise how long the browser tokens remain valid and which port the bundled web interface uses.

## Backend setup

1. Create and activate a virtual environment.
2. Install the Python dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run the LiveKit worker:

   ```bash
   python main.py
   ```

The worker starts the voice assistant and spins up a FastAPI server that serves the React bundle and exposes `/api/livekit/session` for the frontend to fetch the current room credentials.

## Frontend setup

The `frontend/` directory contains a Vite + React application that renders a simple LiveKit call UI. To prepare it for distribution run:

```bash
cd frontend
npm install
npm run build
```

The build output is written to `frontend/dist` and automatically served by the Python process. When the worker records a LiveKit session it exposes the room URL, participant identity, and a short-lived JWT token to the browser so you can join the same call.

### Development loop

If you want hot-module reloading while editing the UI you can run the dev server alongside the Python worker:

```bash
cd frontend
npm run dev -- --host
```

Update `WEB_APP_PORT` if you need the Python process to proxy the production bundle from a different port.

## Packaging

The final distributable consists of the Python worker (with its dependencies from `requirements.txt`) and the prebuilt React bundle in `frontend/dist`. Running `python main.py` will automatically start both the LiveKit worker and the static web server so end users can immediately open `http://localhost:3000` (or your configured port) to join the live session.
