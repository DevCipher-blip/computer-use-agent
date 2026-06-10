import asyncio
import json
import logging
from uuid import UUID

import websockets
import websockets.exceptions
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from config import settings
from database import AsyncSessionLocal
from models import Session, SessionStatus
from security import token_matches
from services.session_manager import agent_headers, wait_until_ready

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stream"])


async def _fail(websocket: WebSocket, message: str, code: int):
    await websocket.send_json({"type": "error", "message": message})
    await websocket.close(code)


@router.websocket("/api/v1/sessions/{session_id}/stream")
async def stream_session(session_id: UUID, websocket: WebSocket, token: str | None = Query(None)):
    """Relay the session container's event stream to the browser.

    Persistence happens in the agent's completion callback, not here, so this
    is a pure relay: any number of viewers (or none) can be connected.
    """
    # Browsers can't set headers on a WebSocket handshake, so the token rides in the query string.
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        row = await db.get(Session, session_id)

    if row is None or not token_matches(row, token):
        return await _fail(websocket, "Session not found", 1008)
    if row.status == SessionStatus.TERMINATED:
        return await _fail(websocket, "Session is terminated", 1008)
    if not row.container_ip:
        return await _fail(websocket, f"Session has no container (status: {row.status.value})", 1011)

    await websocket.send_json({"type": "status", "status": "connecting", "message": "Waiting for container…"})
    if not await wait_until_ready(row.container_ip):
        return await _fail(websocket, "Container failed to become ready", 1011)

    container_ws_url = f"ws://{row.container_ip}:{settings.session_agent_port}/stream"
    await websocket.send_json({"type": "status", "status": "connected", "message": "Container ready"})

    try:
        async with websockets.connect(
            container_ws_url,
            additional_headers=agent_headers(row.agent_token),
            ping_interval=20,
            ping_timeout=30,
            max_size=None,  # tool_result events carry full-size screenshots
        ) as cws:

            async def from_container():
                async for raw in cws:
                    await websocket.send_text(raw)

            async def from_client():
                while True:
                    try:
                        msg = json.loads(await websocket.receive_text())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, dict) and msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})

            tasks = [asyncio.create_task(from_container()), asyncio.create_task(from_client())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, WebSocketDisconnect | websockets.exceptions.ConnectionClosed):
                    raise exc

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Container WS closed for session {session_id}")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Stream error session {session_id}: {e}")
        try:
            await websocket.send_json({"type": "error", "message": "Stream to the session container failed"})
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass
