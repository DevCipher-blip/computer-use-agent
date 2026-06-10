"""
Session Agent — runs inside each isolated container.
Replaces the Streamlit UI; exposes HTTP + WebSocket API consumed by the main backend.
"""

import asyncio
import hmac
import json
import logging
import os
import sys
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

# computer_use_demo is installed in the base image at /home/computeruse
sys.path.insert(0, "/home/computeruse")

from computer_use_demo.loop import APIProvider, sampling_loop  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("session_agent")

_BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000")
# Shared secret with the backend. Session containers sit on one Docker network,
# so without it any container (including another session's) could drive this one.
_AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")


def _token_ok(presented: str | None) -> bool:
    return bool(_AGENT_TOKEN) and hmac.compare_digest(presented or "", _AGENT_TOKEN)


async def require_backend(authorization: str | None = Header(None)):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not _token_ok(token):
        raise HTTPException(status_code=401, detail="Unauthorized")


app = FastAPI(title="Session Agent")

# ── Shared state (single-session container, no multi-tenancy needed here) ──────
_messages: list[dict] = []
_status: str = "idle"
_clients: list[asyncio.Queue] = []
_background_tasks: set[asyncio.Task] = set()


def _broadcast(event: dict):
    """Fan out to every connected stream; also safe from sampling_loop's sync callbacks."""
    data = json.dumps(event, default=str)
    for q in _clients:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


# ── Serialisation helpers ──────────────────────────────────────────────────────


def _ser_block(block: Any) -> dict:
    if isinstance(block, dict):
        return block
    try:
        return dict(block)
    except Exception:
        return {"raw": str(block)}


def _ser_tool_result(result: Any, tool_id: str) -> dict:
    return {
        "type": "tool_result",
        "tool_id": tool_id,
        "output": getattr(result, "output", None),
        "error": getattr(result, "error", None),
        "base64_image": getattr(result, "base64_image", None),
        "system": getattr(result, "system", None),
    }


def _without_images(messages: list[dict]) -> list[dict]:
    """Drop base64 screenshots: they were already streamed as tool_result events
    and would blow past WebSocket frame and database row limits."""
    out = []
    for m in messages:
        content = m.get("content", [])
        if isinstance(content, list):
            content = [
                {k: v for k, v in b.items() if k != "base64_image"} if isinstance(b, dict) else b for b in content
            ]
        out.append({**m, "content": content})
    return out


# ── Agent loop ─────────────────────────────────────────────────────────────────


async def _run_loop(task: str):
    global _status

    turn_start = len(_messages)
    _messages.append({"role": "user", "content": [{"type": "text", "text": task}]})
    _broadcast({"type": "status", "status": "running"})
    turn: list[dict] = []
    error: str | None = None

    try:
        _messages[:] = await sampling_loop(
            model=os.environ.get("MODEL", "claude-sonnet-4-5-20250929"),
            provider=APIProvider.ANTHROPIC,
            system_prompt_suffix="",
            messages=_messages,
            output_callback=lambda block: _broadcast({"type": "output", "data": _ser_block(block)}),
            tool_output_callback=lambda result, tool_id: _broadcast(_ser_tool_result(result, tool_id)),
            api_response_callback=lambda req, resp, err: None,
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            max_tokens=4096,
            tool_version=os.environ.get("TOOL_VERSION", "computer_use_20250124"),
        )
        # Only this task's messages: the backend already has the earlier turns.
        turn = _without_images(_messages[turn_start:])
        _broadcast({"type": "done", "messages": turn, "status": "idle"})
        logger.info("Agent loop completed successfully")
    except Exception as e:
        logger.exception("Agent loop error")
        error = str(e)
        # Drop the half-finished turn so the next task starts from a valid conversation.
        del _messages[turn_start:]
        _broadcast({"type": "error", "message": error, "status": "idle"})
    finally:
        _status = "idle"
        _broadcast({"type": "status", "status": "idle"})
        # Reliably notify backend — works even if no browser is connected.
        await _notify_complete(turn, error)


async def _notify_complete(turn: list[dict], error: str | None):
    session_id = os.environ.get("SESSION_ID", "")
    if not session_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{_BACKEND_URL}/api/v1/sessions/{session_id}/complete",
                headers={"Authorization": f"Bearer {_AGENT_TOKEN}"},
                json={"messages": turn, "error": error},
            )
    except Exception as e:
        logger.warning(f"Could not notify backend of completion: {e}")


# ── HTTP endpoints ─────────────────────────────────────────────────────────────


class RunRequest(BaseModel):
    task: str = Field(..., min_length=1)
    session_id: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "agent_status": _status}


@app.post("/run", dependencies=[Depends(require_backend)])
async def run(body: RunRequest):
    global _status
    if _status == "running":
        return {"error": "already_running", "status": _status}
    # Set before scheduling, so a second /run arriving first can't slip through.
    _status = "running"
    task = asyncio.create_task(_run_loop(body.task))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "started", "task": body.task[:120]}


@app.get("/status", dependencies=[Depends(require_backend)])
async def get_status():
    return {"status": _status, "message_count": len(_messages)}


@app.get("/history", dependencies=[Depends(require_backend)])
async def get_history():
    return {"messages": _without_images(_messages), "status": _status}


# ── WebSocket stream ───────────────────────────────────────────────────────────


@app.websocket("/stream")
async def ws_stream(websocket: WebSocket):
    scheme, _, token = (websocket.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not _token_ok(token):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    _clients.append(queue)

    # Immediately send current status so clients know container state
    await websocket.send_text(json.dumps({"type": "status", "status": _status}))

    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=25.0)
                await websocket.send_text(data)
            except TimeoutError:
                # Heartbeat to keep connection alive
                await websocket.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, RuntimeError) as e:
        logger.debug(f"WS client disconnected: {e}")
    finally:
        if queue in _clients:
            _clients.remove(queue)


if __name__ == "__main__":
    port = int(os.environ.get("SESSION_AGENT_PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
