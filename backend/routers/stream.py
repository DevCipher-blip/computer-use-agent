import asyncio
import json
import logging

import websockets
import websockets.exceptions
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal
from models import Session, SessionStatus
from services.session_manager import _wait_ready, save_assistant_message

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stream"])


@router.websocket("/api/v1/sessions/{session_id}/stream")
async def stream_session(session_id: str, websocket: WebSocket):
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one_or_none()

    if not row:
        await websocket.send_json({"type": "error", "message": "Session not found"})
        await websocket.close(1008)
        return

    if row.status == SessionStatus.TERMINATED:
        await websocket.send_json({"type": "error", "message": "Session is terminated"})
        await websocket.close(1008)
        return

    container_ip = row.container_ip
    await websocket.send_json({"type": "status", "status": "connecting", "message": "Waiting for container…"})

    ready = await _wait_ready(container_ip)
    if not ready:
        await websocket.send_json({"type": "error", "message": "Container failed to become ready"})
        await websocket.close(1011)
        return

    container_ws_url = f"ws://{container_ip}:{settings.session_agent_port}/stream"
    await websocket.send_json({"type": "status", "status": "connected", "message": "Container ready"})

    try:
        async with websockets.connect(container_ws_url, ping_interval=20, ping_timeout=30) as cws:

            async def forward_from_container():
                async for raw in cws:
                    try:
                        event = json.loads(raw)
                        if event.get("type") == "done":
                            msgs = event.get("messages", [])
                            assistant_blocks = []
                            for m in msgs:
                                if m.get("role") == "assistant":
                                    assistant_blocks.extend(m.get("content", []))
                            if assistant_blocks:
                                await save_assistant_message(session_id, assistant_blocks)
                        await websocket.send_text(raw)
                    except Exception as e:
                        logger.debug(f"Forward error: {e}")

            async def forward_from_client():
                try:
                    while True:
                        data = await websocket.receive_text()
                        msg = json.loads(data)
                        if msg.get("type") == "ping":
                            await websocket.send_json({"type": "pong"})
                except (WebSocketDisconnect, Exception):
                    pass

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(forward_from_container()),
                    asyncio.create_task(forward_from_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Container WS closed for session {session_id}")
    except Exception as e:
        logger.error(f"Stream error session {session_id}: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
