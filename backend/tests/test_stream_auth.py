import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from main import app


@pytest.fixture
def client(docker_client):
    # No `with`: the lifespan would try to reach Postgres and recover ports.
    return TestClient(app)


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
