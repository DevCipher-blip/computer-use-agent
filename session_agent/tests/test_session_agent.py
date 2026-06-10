"""Session agent tests with a fake sampling loop: no Anthropic calls, no desktop."""

import asyncio
import importlib
import sys
import types
from pathlib import Path

import httpx
import pytest

AGENT_DIR = Path(__file__).resolve().parent.parent
AGENT_TOKEN = "a" * 64


class FakeLoop:
    """Replaces computer_use_demo.loop.sampling_loop and records what it was given."""

    def __init__(self):
        self.calls = []
        self.fail_with = None
        self.release = None

    async def __call__(self, *, messages, output_callback, tool_output_callback, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        if self.release:
            await self.release.wait()
        if self.fail_with:
            raise self.fail_with
        output_callback({"type": "text", "text": "Taking a screenshot"})
        tool_output_callback(types.SimpleNamespace(output=None, error=None, base64_image="iVBORw0KGgo="), "toolu_1")
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "Done: it is sunny"}]})
        messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "base64_image": "iVBORw0KGgo="}],
            }
        )
        return messages


@pytest.fixture
def agent(monkeypatch):
    fake = FakeLoop()
    loop_mod = types.ModuleType("computer_use_demo.loop")
    loop_mod.APIProvider = types.SimpleNamespace(ANTHROPIC="anthropic")
    loop_mod.sampling_loop = fake
    monkeypatch.setitem(sys.modules, "computer_use_demo", types.ModuleType("computer_use_demo"))
    monkeypatch.setitem(sys.modules, "computer_use_demo.loop", loop_mod)
    monkeypatch.setenv("AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("SESSION_ID", "3f65d497-de20-4e18-bc28-31a9a2f02618")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.syspath_prepend(str(AGENT_DIR))
    sys.modules.pop("session_agent", None)
    module = importlib.import_module("session_agent")

    callbacks = []

    async def fake_notify(turn, error):
        callbacks.append((turn, error))

    monkeypatch.setattr(module, "_notify_complete", fake_notify)
    module.fake_loop = fake
    module.callbacks = callbacks
    yield module
    sys.modules.pop("session_agent", None)


AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}


async def _client(module):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://agent")


async def _wait_idle(module):
    for _ in range(100):
        if not module._background_tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


async def test_health_is_open_but_everything_else_needs_the_agent_token(agent):
    async with await _client(agent) as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.post("/run", json={"task": "x"})).status_code == 401
        assert (await c.post("/run", json={"task": "x"}, headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await c.get("/history")).status_code == 401
        assert (await c.get("/status", headers=AUTH)).status_code == 200


async def test_run_reports_only_the_new_turn_without_screenshots(agent):
    queue = asyncio.Queue()
    agent._clients.append(queue)
    async with await _client(agent) as c:
        assert (await c.post("/run", json={"task": "weather in Dubai"}, headers=AUTH)).json()["status"] == "started"
        await _wait_idle(agent)
        assert (await c.post("/run", json={"task": "and in Oslo"}, headers=AUTH)).json()["status"] == "started"
        await _wait_idle(agent)

    turn, error = agent.callbacks[1]
    assert error is None
    assert [m["role"] for m in turn] == ["user", "assistant", "user"]
    assert turn[0]["content"][0]["text"] == "and in Oslo"
    assert "base64_image" not in turn[2]["content"][0]

    events = []
    while not queue.empty():
        events.append(__import__("json").loads(queue.get_nowait()))
    kinds = [e["type"] for e in events]
    assert kinds[:5] == ["status", "output", "tool_result", "done", "status"]
    assert events[2]["base64_image"] == "iVBORw0KGgo="
    # The second run saw the whole conversation, so the model keeps context.
    assert len(agent.fake_loop.calls[1]["messages"]) == 4


async def test_second_run_while_busy_is_refused(agent):
    agent.fake_loop.release = asyncio.Event()
    async with await _client(agent) as c:
        first = await c.post("/run", json={"task": "one"}, headers=AUTH)
        second = await c.post("/run", json={"task": "two"}, headers=AUTH)
        assert first.json()["status"] == "started"
        assert second.json() == {"error": "already_running", "status": "running"}
        agent.fake_loop.release.set()
        await _wait_idle(agent)
    assert len(agent.fake_loop.calls) == 1


async def test_failed_run_is_rolled_back_and_reported(agent):
    agent.fake_loop.fail_with = RuntimeError("overloaded")
    async with await _client(agent) as c:
        await c.post("/run", json={"task": "one"}, headers=AUTH)
        await _wait_idle(agent)
        history = (await c.get("/history", headers=AUTH)).json()
    assert agent.callbacks == [([], "overloaded")]
    assert history == {"messages": [], "status": "idle"}


async def test_completion_callback_carries_the_agent_token(agent, monkeypatch):
    sys.modules.pop("session_agent", None)
    module = importlib.import_module("session_agent")
    sent = {}

    def handler(request):
        sent["url"] = str(request.url)
        sent["auth"] = request.headers["authorization"]
        sent["body"] = request.content
        return httpx.Response(200, json={"status": "ok"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    await module._notify_complete([{"role": "assistant", "content": []}], None)
    assert sent["url"].endswith("/api/v1/sessions/3f65d497-de20-4e18-bc28-31a9a2f02618/complete")
    assert sent["auth"] == f"Bearer {AGENT_TOKEN}"
    assert b'"role":"assistant"' in sent["body"]


def test_stream_rejects_connections_without_the_token(agent):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(agent.app)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/stream") as ws:
        ws.receive_text()
    with client.websocket_connect("/stream", headers=AUTH) as ws:
        assert ws.receive_json() == {"type": "status", "status": "idle"}
