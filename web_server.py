"""Embedded web server for the Codex Belya voice assistant."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from threading import Lock, Thread
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from livekit.api import AccessToken, VideoGrants

from session_store import SessionStore

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
_DIST_DIR = _FRONTEND_DIR / "dist"
_DEFAULT_PORT = 3000

_session_store = SessionStore()
_api_router = APIRouter(prefix="/api")


def _resolve_port() -> int:
    port_env = os.getenv("WEB_APP_PORT")
    if not port_env:
        return _DEFAULT_PORT
    try:
        return int(port_env)
    except ValueError:
        logger.warning("Invalid WEB_APP_PORT=%s; falling back to %s", port_env, _DEFAULT_PORT)
        return _DEFAULT_PORT


def _build_token(identity: str, room: str, name: Optional[str] = None) -> str:
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    if not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="LiveKit API credentials are not configured")

    ttl_seconds = int(os.getenv("LIVEKIT_TOKEN_TTL", "3600"))

    token = (
        AccessToken(api_key=api_key, api_secret=api_secret)
        .with_identity(identity)
        .with_name(name or identity)
        .with_ttl(ttl_seconds)
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
    )
    return token.to_jwt()


@_api_router.get("/livekit/session")
async def fetch_livekit_session() -> JSONResponse:
    state = _session_store.get_livekit_state()
    logger.debug("Loaded stored LiveKit state: %s", state)
    if not state:
        raise HTTPException(status_code=404, detail="No LiveKit session has been recorded yet")

    participant_identity = state.get("participant_identity") or state.get("participant_sid")
    room_name = state.get("room_name") or state.get("room_sid") or state.get("room_id")

    if not participant_identity or not room_name:
        raise HTTPException(status_code=404, detail="LiveKit session information is incomplete")

    server_url = os.getenv("LIVEKIT_URL")
    if not server_url:
        raise HTTPException(status_code=500, detail="LIVEKIT_URL is not configured")

    token = _build_token(participant_identity, room_name)

    payload = {
        "identity": participant_identity,
        "room": room_name,
        "url": server_url,
        "token": token,
    }
    return JSONResponse(payload)


def _create_app() -> FastAPI:
    app = FastAPI(title="Codex Belya Web UI")
    app.include_router(_api_router)

    if _DIST_DIR.exists():
        app.mount("/", StaticFiles(directory=_DIST_DIR, html=True), name="frontend")
        logger.info("Serving compiled frontend from %s", _DIST_DIR)
    else:
        logger.warning("Frontend build directory %s not found. API endpoints remain available.", _DIST_DIR)

        @app.get("/")
        async def _frontend_missing() -> JSONResponse:  # pragma: no cover - trivial HTTP response
            return JSONResponse(
                {
                    "detail": "Frontend build not found. Run 'npm install' and 'npm run build' in the frontend directory.",
                }
            )

    return app


_app = _create_app()
_server_thread: Optional[Thread] = None
_server_lock = Lock()


def ensure_web_app_started() -> None:
    """Start the bundled FastAPI server in a background thread if needed."""

    global _server_thread

    if _server_thread and _server_thread.is_alive():
        return

    with _server_lock:
        if _server_thread and _server_thread.is_alive():
            return

        port = _resolve_port()

        try:
            import uvicorn
        except ImportError as error:  # pragma: no cover - dependency issue
            logger.error("uvicorn is required to serve the web frontend: %s", error)
            return

        config = uvicorn.Config(
            app=_app,
            host=os.getenv("WEB_APP_HOST", "0.0.0.0"),
            port=port,
            log_level=os.getenv("WEB_APP_LOG_LEVEL", "info"),
        )

        server = uvicorn.Server(config)

        def _run() -> None:
            logger.info("Starting web UI on http://%s:%s", config.host, config.port)
            asyncio.set_event_loop(asyncio.new_event_loop())
            server.run()

        _server_thread = Thread(target=_run, name="web-ui-server", daemon=True)
        _server_thread.start()


__all__ = ["ensure_web_app_started", "_app"]
