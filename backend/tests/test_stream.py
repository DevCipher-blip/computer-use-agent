import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import routers.stream as stream
from main import app


@pytest.fixture
def client(docker_client):
    # No `with`: the lifespan would try to reach Postgres and recover ports.
    return TestClient(app)


@pytest.fixture
def container(monkeypatch):
    """Stand-in for the session container's /stream WebSocket."""

    class FakeContainerSocket:
        def __init__(self):
            self.events = []
            self.connected_with = None

        def __call__(self, url, **kwargs):
            self.connected_with = (url, kwargs)
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            async def gen():
                for e in self.events:
                    yield json.dumps(e)

            return gen()

    fake = FakeContainerSocket()

    async def ready(_ip, timeout=None):
        return True

    monkeypatch.setattr(stream.websockets, "connect", fake)
    monkeypatch.setattr(stream, "wait_until_ready", ready)
    return fake


def _create(client):
    data = client.post("/api/v1/sessions", json={"api_key": "test-key-not-real"}).json()
    return data["id"], data["access_token"]


@pytest.mark.parametrize("query", ["", "?token=", "?token=wrong"])
def test_stream_refuses_without_the_sessions_token(client, query):
    sid, _ = _create(client)
    with client.websocket_connect(f"/api/v1/sessions/{sid}/stream{query}") as ws:
        assert ws.receive_json() == {"type": "error", "message": "Session not found"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1008


def test_stream_refuses_a_non_uuid_id(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/sessions/not-a-uuid/stream?token=x") as ws:
            ws.receive_json()


def test_stream_relays_container_events_in_order(client, container, docker_client):
    sid, token = _create(client)
    container.events = [
        {"type": "status", "status": "running"},
        {"type": "output", "data": {"type": "text", "text": "Taking a screenshot"}},
        {"type": "tool_result", "tool_id": "toolu_1", "output": None, "base64_image": "iVBORw0KGgo="},
        {"type": "done", "messages": [], "status": "idle"},
    ]
    with client.websocket_connect(f"/api/v1/sessions/{sid}/stream?token={token}") as ws:
        assert ws.receive_json()["status"] == "connecting"
        assert ws.receive_json()["status"] == "connected"
        assert [ws.receive_json() for _ in container.events] == container.events

    url, kwargs = container.connected_with
    assert url == "ws://10.9.0.7:8765/stream"
    agent_token = docker_client.containers.run.call_args.kwargs["environment"]["AGENT_TOKEN"]
    assert kwargs["additional_headers"] == {"Authorization": f"Bearer {agent_token}"}


def test_stream_does_not_persist_anything_itself(client, container):
    sid, token = _create(client)
    container.events = [{"type": "done", "messages": [{"role": "assistant", "content": []}]}]
    with client.websocket_connect(f"/api/v1/sessions/{sid}/stream?token={token}") as ws:
        for _ in range(3):
            ws.receive_json()
    history = client.get(
        f"/api/v1/sessions/{sid}/history", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert history["messages"] == []


def test_stream_fails_fast_for_a_session_without_a_container(client, docker_client, monkeypatch):
    container = docker_client.containers.run.return_value
    container.attrs = {"NetworkSettings": {"Networks": {"computeagent_network": {"IPAddress": ""}}}}
    sid, token = _create(client)

    async def never_ready(_ip, timeout=None):
        raise AssertionError("should not wait for a container it cannot reach")

    monkeypatch.setattr(stream, "wait_until_ready", never_ready)
    with client.websocket_connect(f"/api/v1/sessions/{sid}/stream?token={token}") as ws:
        assert "no container" in ws.receive_json()["message"]
