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


def agent_auth(docker_client):
    """Headers the most recently spawned container would use for its callback."""
    env = docker_client.containers.run.call_args.kwargs["environment"]
    return {"Authorization": f"Bearer {env['AGENT_TOKEN']}"}


async def test_create_spawns_one_container_and_reports_vnc(api, docker_client):
    r = await create(api)
    assert r.status_code == 201
    data = r.json()
    assert data["status"] == "idle"
    assert data["vnc_info"]["vnc_port"] == 5901
    base = f"novnc/{data['id']}"
    assert data["vnc_info"]["novnc_url"] == (
        f"/{base}/vnc.html?autoconnect=true&resize=scale&path={base}/websockify"
    )

    docker_client.containers.run.assert_called_once()
    kwargs = docker_client.containers.run.call_args.kwargs
    assert kwargs["image"] == "computeagent/session-agent:latest"
    assert kwargs["environment"]["SESSION_ID"] == data["id"]
    assert kwargs["environment"]["ANTHROPIC_API_KEY"] == "test-key-not-real"
    assert kwargs["labels"]["computeagent.session_id"] == data["id"]
    # The container gets its own agent secret, never the user's session token.
    assert data["access_token"] not in kwargs["environment"].values()
    assert len(kwargs["environment"]["AGENT_TOKEN"]) == 64


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


async def test_message_runs_session_and_rejects_while_busy(api, docker_client, no_dispatch):
    sid, auth = await new_session(api)

    r = await api.post(f"{SESSIONS}/{sid}/message", json={"text": "open the calculator"}, headers=auth)
    assert r.status_code == 202
    assert r.json()["content"] == [{"type": "text", "text": "open the calculator"}]
    await asyncio.sleep(0)
    assert [(str(s), ip, t) for s, ip, t in no_dispatch] == [(sid, "10.9.0.7", "open the calculator")]

    assert (await api.get(f"{SESSIONS}/{sid}", headers=auth)).json()["status"] == "running"
    busy = await api.post(f"{SESSIONS}/{sid}/message", json={"text": "again"}, headers=auth)
    assert busy.status_code == 400

    done = await api.post(f"{SESSIONS}/{sid}/complete", json={}, headers=agent_auth(docker_client))
    assert done.status_code == 200
    assert (await api.get(f"{SESSIONS}/{sid}", headers=auth)).json()["status"] == "idle"


async def test_history_lists_user_messages_in_order(api, docker_client, no_dispatch):
    sid, auth = await new_session(api)
    await api.post(f"{SESSIONS}/{sid}/message", json={"text": "first"}, headers=auth)
    await api.post(f"{SESSIONS}/{sid}/complete", json={}, headers=agent_auth(docker_client))
    await api.post(f"{SESSIONS}/{sid}/message", json={"text": "second"}, headers=auth)

    history = (await api.get(f"{SESSIONS}/{sid}/history", headers=auth)).json()
    assert [m["content"][0]["text"] for m in history["messages"]] == ["first", "second"]


def _turn(*texts):
    """A finished task as the session agent reports it: user prompt, assistant replies, tool results."""
    return [
        {"role": "user", "content": [{"type": "text", "text": "task"}]},
        *({"role": "assistant", "content": [{"type": "text", "text": t}]} for t in texts),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": []}]},
    ]


async def test_completion_stores_the_assistant_turn_once(api, docker_client, no_dispatch):
    sid, auth = await new_session(api)
    agent = agent_auth(docker_client)
    await api.post(f"{SESSIONS}/{sid}/message", json={"text": "check the weather"}, headers=auth)

    body = {"messages": _turn("Opening Firefox", "It is 31C in Dubai")}
    assert (await api.post(f"{SESSIONS}/{sid}/complete", json=body, headers=agent)).status_code == 200
    # A retried callback must not duplicate the answer.
    await api.post(f"{SESSIONS}/{sid}/complete", json=body, headers=agent)

    history = (await api.get(f"{SESSIONS}/{sid}/history", headers=auth)).json()["messages"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert [b["text"] for b in history[1]["content"]] == ["Opening Firefox", "It is 31C in Dubai"]
    assert (await api.get(f"{SESSIONS}/{sid}", headers=auth)).json()["status"] == "idle"


async def test_failed_run_is_recorded_and_session_freed(api, docker_client, no_dispatch):
    sid, auth = await new_session(api)
    await api.post(f"{SESSIONS}/{sid}/message", json={"text": "x"}, headers=auth)
    body = {"messages": [], "error": "overloaded"}
    await api.post(f"{SESSIONS}/{sid}/complete", json=body, headers=agent_auth(docker_client))

    history = (await api.get(f"{SESSIONS}/{sid}/history", headers=auth)).json()["messages"]
    assert history[-1]["content"] == [{"type": "text", "text": "Run failed: overloaded"}]
    assert (await api.get(f"{SESSIONS}/{sid}", headers=auth)).json()["status"] == "idle"


async def test_completion_callback_only_accepts_the_agent_secret(api, docker_client, no_dispatch):
    sid, auth = await new_session(api)
    other_agent = agent_auth(docker_client)
    _, _ = await new_session(api)
    await api.post(f"{SESSIONS}/{sid}/message", json={"text": "x"}, headers=auth)
    url = f"{SESSIONS}/{sid}/complete"

    assert (await api.post(url, json={})).status_code == 401
    # Neither the browser's token nor another container's secret can mark it done.
    assert (await api.post(url, json={}, headers=auth)).status_code == 404
    assert (await api.post(url, json={}, headers=agent_auth(docker_client))).status_code == 404
    assert (await api.get(f"{SESSIONS}/{sid}", headers=auth)).json()["status"] == "running"
    assert (await api.post(url, json={}, headers=other_agent)).status_code == 200


async def test_delete_stops_container_and_terminates(api, docker_client):
    sid, auth = await new_session(api)
    r = await api.delete(f"{SESSIONS}/{sid}", headers=auth)
    assert r.status_code == 204
    docker_client.containers.get.assert_called_with("c0ffee")
    docker_client.containers.get.return_value.remove.assert_called_once_with(force=True)
    after = (await api.get(f"{SESSIONS}/{sid}", headers=auth)).json()
    assert after["status"] == "terminated"
    assert after["vnc_info"] is None

    # A second delete is a no-op, not another Docker call.
    assert (await api.delete(f"{SESSIONS}/{sid}", headers=auth)).status_code == 204
    assert docker_client.containers.get.call_count == 1


async def test_unknown_session_is_404(api):
    _, auth = await new_session(api)
    missing = uuid.uuid4()
    assert (await api.get(f"{SESSIONS}/{missing}", headers=auth)).status_code == 404
    assert (await api.get(f"{SESSIONS}/{missing}/history", headers=auth)).status_code == 404
    assert (await api.delete(f"{SESSIONS}/{missing}", headers=auth)).status_code == 404


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("get", "/history"), ("delete", ""), ("post", "/message")],
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


async def test_session_limit_is_enforced_and_terminating_frees_a_slot(api, docker_client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "max_concurrent_sessions", 2)
    first, first_auth = await new_session(api)
    await new_session(api)

    refused = await create(api)
    assert refused.status_code == 429
    assert "limit 2" in refused.json()["detail"]
    assert docker_client.containers.run.call_count == 2

    await api.delete(f"{SESSIONS}/{first}", headers=first_auth)
    assert (await create(api)).status_code == 201


async def test_failed_sessions_do_not_count_against_the_limit(api, docker_client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "max_concurrent_sessions", 1)
    docker_client.containers.run.side_effect = docker.errors.APIError("boom")
    assert (await create(api)).status_code == 500
    docker_client.containers.run.side_effect = None
    assert (await create(api)).status_code == 201


async def test_simultaneous_messages_start_only_one_run(api, no_dispatch):
    sid, auth = await new_session(api)
    url = f"{SESSIONS}/{sid}/message"
    results = await asyncio.gather(
        *(api.post(url, json={"text": f"task {i}"}, headers=auth) for i in range(5))
    )
    assert sorted(r.status_code for r in results) == [202, 400, 400, 400, 400]
    await asyncio.sleep(0)
    assert len(no_dispatch) == 1


async def test_startup_recovery_fails_sessions_stuck_creating(api):
    from database import AsyncSessionLocal
    from models import Session, SessionStatus
    from services.session_manager import recover_active_sessions

    async with AsyncSessionLocal() as db:
        db.add(Session(access_token_hash="0" * 64, agent_token="1" * 64, status=SessionStatus.CREATING))
        await db.commit()

    await recover_active_sessions()

    async with AsyncSessionLocal() as db:
        statuses = [s.status for s in await db.scalars(select(Session))]
    assert statuses == [SessionStatus.ERROR]


async def test_cross_origin_requests_are_not_allowed_by_default(api):
    r = await api.options(
        SESSIONS, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in r.headers
