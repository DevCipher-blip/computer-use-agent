"""
Session Agent — runs inside each isolated container.
Replaces the Streamlit UI; exposes HTTP + WebSocket API consumed by the main backend.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# computer_use_demo is installed in the base image at /home/computeruse
sys.path.insert(0, "/home/computeruse")

from computer_use_demo.loop import APIProvider, sampling_loop  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("session_agent")

_BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000")


async def _notify_complete():
    session_id = os.environ.get("SESSION_ID", "")
    if not session_id:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{_BACKEND_URL}/api/v1/sessions/{session_id}/complete")
    except Exception as e:
        logger.warning(f"Could not notify backend of completion: {e}")

app = FastAPI(title="Session Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Shared state (single-session container, no multi-tenancy needed here) ──────
_messages: list[dict] = []
_status: str = "idle"
_clients: list[asyncio.Queue] = []
_run_lock = asyncio.Lock()


# ── Broadcasting ───────────────────────────────────────────────────────────────

def _broadcast_sync(event: dict):
    """Safe to call from sync callbacks inside sampling_loop (same event loop)."""
    data = json.dumps(event, default=str)
    for q in list(_clients):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


async def _broadcast(event: dict):
    data = json.dumps(event, default=str)
    for q in list(_clients):
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


# ── Agent loop ─────────────────────────────────────────────────────────────────

async def _run_loop(task: str):
    global _messages, _status

    async with _run_lock:
        _status = "running"

    _messages.append({"role": "user", "content": [{"type": "text", "text": task}]})
    await _broadcast({"type": "status", "status": "running"})

    try:
        def output_cb(block):
            _broadcast_sync({"type": "output", "data": _ser_block(block)})

        def tool_cb(result, tool_id: str):
            _broadcast_sync(_ser_tool_result(result, tool_id))

        def api_cb(req, resp, err):
            pass

        _messages[:] = await sampling_loop(
            model=os.environ.get("MODEL", "claude-sonnet-4-5-20250929"),
            provider=APIProvider.ANTHROPIC,
            system_prompt_suffix="",
            messages=_messages,
            output_callback=output_cb,
            tool_output_callback=tool_cb,
            api_response_callback=api_cb,
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            max_tokens=4096,
            tool_version=os.environ.get("TOOL_VERSION", "computer_use_20251124"),
        )

        # Strip base64 screenshots before broadcasting — they're already streamed
        # individually via tool_result events and would exceed WS frame limits
        def _strip_b64(msgs):
            out = []
            for m in msgs:
                blocks = []
                for b in m.get("content", []):
                    if isinstance(b, dict) and b.get("base64_image"):
                        b = {k: v for k, v in b.items() if k != "base64_image"}
                    blocks.append(b)
                out.append({**m, "content": blocks})
            return out

        await _broadcast({"type": "done", "messages": _strip_b64(_messages), "status": "idle"})
        logger.info("Agent loop completed successfully")

    except Exception as e:
        logger.exception("Agent loop error")
        await _broadcast({"type": "error", "message": str(e), "status": "idle"})
    finally:
        async with _run_lock:
            _status = "idle"
        await _broadcast({"type": "status", "status": "idle"})
        # Reliably notify backend — works even if WebSocket dropped
        await _notify_complete()


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    task: str
    session_id: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "agent_status": _status}


@app.post("/run")
async def run(body: RunRequest):
    if _status == "running":
        return {"error": "already_running", "status": _status}
    asyncio.create_task(_run_loop(body.task))
    return {"status": "started", "task": body.task[:120]}


@app.get("/status")
async def get_status():
    return {"status": _status, "message_count": len(_messages)}


@app.get("/history")
async def get_history():
    return {"messages": _messages, "status": _status}


# ── WebSocket stream ───────────────────────────────────────────────────────────

@app.websocket("/stream")
async def ws_stream(websocket: WebSocket):
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
            except asyncio.TimeoutError:
                # Heartbeat to keep connection alive
                await websocket.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, Exception) as e:
        logger.debug(f"WS client disconnected: {e}")
    finally:
        if queue in _clients:
            _clients.remove(queue)


if __name__ == "__main__":
    port = int(os.environ.get("SESSION_AGENT_PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
