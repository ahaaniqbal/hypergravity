"""Serves the pill and the verification panel.

Runs as a background task inside the bot's own event loop, so it shares the
event bus directly — no IPC, no second process to keep alive during a demo.

    http://localhost:7861/          verification panel (second screen)
    http://localhost:7861/?pill     the pill (small always-on-top window)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from loguru import logger

from . import events

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
PORT = 7861


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse((UI_DIR / "index.html").read_text())

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        # Replay first so a judge opening the panel mid-call sees the story.
        if backlog := events.history():
            await sock.send_text(json.dumps(backlog))

        q = events.subscribe()
        try:
            while True:
                await sock.send_text(json.dumps(await q.get()))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            events.unsubscribe(q)

    return app


async def serve() -> None:
    """Start the UI alongside the bot. Failure here must never kill a call."""
    config = uvicorn.Config(build_app(), host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    try:
        logger.info(f"UI at http://localhost:{PORT}/  (pill: /?pill)")
        await server.serve()
    except SystemExit as e:
        # uvicorn calls sys.exit(3) when it can't bind. Left uncaught that
        # propagates out of the task and takes the whole bot down with it —
        # the caller hears a greeting and then a dead line. A dashboard that
        # won't start is a cosmetic problem; it must never end a phone call.
        logger.warning(f"UI server couldn't start (exit {e.code}) — carrying on without it")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"UI server stopped: {e}")


_task: asyncio.Task | None = None


def start() -> asyncio.Task | None:
    """Start once per process, not once per call.

    ``run_bot`` runs for every incoming call, so calling this from there bound
    the port on the first call and then failed on every one after it.
    """
    global _task
    if _task and not _task.done():
        return _task
    _task = asyncio.create_task(serve())
    return _task
