import asyncio
import uuid

import docker.errors
import pytest
from sqlalchemy import select

SESSIONS = "/api/v1/sessions"


async def create(api, **overrides):
    body = {"api_key": "test-key-not-real", **overrides}
    return await api.post(SESSIONS, json=body)


async def new_session(api):
    """Create a session; return (id, auth headers)."""
    data = (await create(api)).json()
    return data["id"], {"Authorization": f"Bearer {data['access_token']}"}


async def test_create_spawns_one_container_and_reports_vnc(api, docker_client):
    r = await create(api)
    assert r.status_code == 201
    data = r.json()
    assert data["status"] == "idle"
    assert data["vnc_info"]["vnc_port"] == 5901

    docker_client.containers.run.assert_called_once()
    kwargs = docker_client.containers.run.call_args.kwargs
    assert kwargs["image"] == "computeagent/session-agent:latest"
    assert kwargs["environment"]["SESSION_ID"] == data["id"]
    assert kwargs["environment"]["ANTHROPIC_API_KEY"] == "test-key-not-real"
    assert kwargs["labels"]["computeagent.session_id"] == data["id"]
    assert kwargs["environment"]["SESSION_TOKEN"] == data["access_token"]


async def test_api_key_is_not_returned_or_stored(api):
    r = await create(api)
    assert "test-key-not-real" not in r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    detail = await api.get(f"{SESSIONS}/{r.json()['id']}", headers=headers)
    assert detail.status_code == 200
    assert "test-key-not-real" not in detail.text
    assert "access_token" not in detail.json()


async def test_spawn_failure_marks_session_error(api, docker_client):
    docker_client.containers.run.side_effect = docker.errors.APIError("boom")
    r = await create(api)
    assert r.status_code == 500

    from database import AsyncSessionLocal
    from models import Session

    async with AsyncSessionLocal() as db:
        statuses = [s.status.value for s in await db.scalars(select(Session))]
    assert statuses == ["error"]


async def test_message_runs_session_and_rejects_while_busy(api, no_dispatch):
    sid, auth = await new_session(api)

    r = await api.post(f"{SESSIONS}/{sid}/message", json={"text": "open the calculator"}, headers=auth)
    assert r.status_code == 202
    assert r.json()["content"] == [{"type": "text", "text": "open the calculator"}]
    await asyncio.sleep(0)
    assert [(str(s), ip, t) for s, ip, t in no_dispatch] == [(sid, "10.9.0.7", "open the calculator")]

    assert (await api.get(f"{SESSIONS}/{sid}", headers=auth)).json()["status"] == "running"
    busy = await api.post(f"{SESSIONS}/{sid}/message", json={"text": "again"}, headers=auth)
    assert busy.status_code == 400

    done = await api.post(f"{SESSIONS}/{sid}/complete", headers=auth)
    assert done.status_code == 200
    assert (await api.get(f"{SESSIONS}/{sid}", headers=auth)).json()["status"] == "idle"


async def test_history_lists_user_messages_in_order(api, no_dispatch):
    sid, auth = await new_session(api)
    await api.post(f"{SESSIONS}/{sid}/message", json={"text": "first"}, headers=auth)
    await api.post(f"{SESSIONS}/{sid}/complete", headers=auth)
    await api.post(f"{SESSIONS}/{sid}/message", json={"text": "second"}, headers=auth)

    history = (await api.get(f"{SESSIONS}/{sid}/history", headers=auth)).json()
    assert [m["content"][0]["text"] for m in history["messages"]] == ["first", "second"]


async def test_delete_stops_container_and_terminates(api, docker_client):
    sid, auth = await new_session(api)
    r = await api.delete(f"{SESSIONS}/{sid}", headers=auth)
    assert r.status_code == 204
    docker_client.containers.get.assert_called_with("c0ffee")
    docker_client.containers.get.return_value.remove.assert_called_once_with(force=True)
    assert (await api.get(f"{SESSIONS}/{sid}", headers=auth)).json()["status"] == "terminated"


async def test_unknown_session_is_404(api):
    _, auth = await new_session(api)
    missing = uuid.uuid4()
    assert (await api.get(f"{SESSIONS}/{missing}", headers=auth)).status_code == 404
    assert (await api.get(f"{SESSIONS}/{missing}/history", headers=auth)).status_code == 404
    assert (await api.delete(f"{SESSIONS}/{missing}", headers=auth)).status_code == 404


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("get", "/history"), ("delete", ""), ("post", "/complete"), ("post", "/message")],
)
async def test_session_routes_need_that_sessions_token(api, no_dispatch, method, suffix):
    sid, _ = await new_session(api)
    _, other_auth = await new_session(api)
    url = f"{SESSIONS}/{sid}{suffix}"
    kwargs = {"json": {"text": "x"}} if suffix == "/message" else {}

    assert (await getattr(api, method)(url, **kwargs)).status_code == 401
    wrong = await getattr(api, method)(url, headers=other_auth, **kwargs)
    assert wrong.status_code == 404
    forged = await getattr(api, method)(url, headers={"Authorization": "Bearer guess"}, **kwargs)
    assert forged.status_code == 404
    assert no_dispatch == []


async def test_there_is_no_endpoint_listing_every_session(api):
    await new_session(api)
    assert (await api.get(SESSIONS)).status_code == 405


async def test_only_the_token_hash_is_stored(api):
    r = await create(api)
    from database import AsyncSessionLocal
    from models import Session

    async with AsyncSessionLocal() as db:
        row = await db.get(Session, uuid.UUID(r.json()["id"]))
    assert row.access_token_hash != r.json()["access_token"]
    assert len(row.access_token_hash) == 64


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": ""},
        {"api_key": "k", "provider": "bedrock"},
        {"api_key": "k", "tool_version": "computer_use_19990101"},
        {"api_key": "k", "model": "gpt-4o"},
        {"api_key": "k", "model": "claude-x; rm -rf /"},
    ],
)
async def test_create_rejects_bad_input_before_spawning(api, docker_client, body):
    assert (await api.post(SESSIONS, json=body)).status_code == 422
    docker_client.containers.run.assert_not_called()


async def test_empty_task_is_rejected(api, no_dispatch):
    sid, auth = await new_session(api)
    r = await api.post(f"{SESSIONS}/{sid}/message", json={"text": ""}, headers=auth)
    assert r.status_code == 422


async def test_health(api):
    assert (await api.get("/health")).json() == {"status": "ok"}
