import asyncio
import uuid

import docker.errors

SESSIONS = "/api/v1/sessions"


async def create(api, **overrides):
    body = {"api_key": "test-key-not-real", **overrides}
    return await api.post(SESSIONS, json=body)


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


async def test_api_key_is_not_returned_or_stored(api):
    r = await create(api)
    assert "test-key-not-real" not in r.text
    detail = await api.get(f"{SESSIONS}/{r.json()['id']}")
    assert "test-key-not-real" not in detail.text


async def test_spawn_failure_marks_session_error(api, docker_client):
    docker_client.containers.run.side_effect = docker.errors.APIError("boom")
    r = await create(api)
    assert r.status_code == 500
    listing = (await api.get(SESSIONS)).json()
    assert [s["status"] for s in listing["sessions"]] == ["error"]


async def test_message_runs_session_and_rejects_while_busy(api, no_dispatch):
    sid = (await create(api)).json()["id"]

    r = await api.post(f"{SESSIONS}/{sid}/message", json={"text": "open the calculator"})
    assert r.status_code == 202
    assert r.json()["content"] == [{"type": "text", "text": "open the calculator"}]
    await asyncio.sleep(0)
    assert [(str(s), ip, t) for s, ip, t in no_dispatch] == [(sid, "10.9.0.7", "open the calculator")]

    assert (await api.get(f"{SESSIONS}/{sid}")).json()["status"] == "running"
    busy = await api.post(f"{SESSIONS}/{sid}/message", json={"text": "again"})
    assert busy.status_code == 400

    done = await api.post(f"{SESSIONS}/{sid}/complete")
    assert done.status_code == 200
    assert (await api.get(f"{SESSIONS}/{sid}")).json()["status"] == "idle"


async def test_history_lists_user_messages_in_order(api, no_dispatch):
    sid = (await create(api)).json()["id"]
    await api.post(f"{SESSIONS}/{sid}/message", json={"text": "first"})
    await api.post(f"{SESSIONS}/{sid}/complete")
    await api.post(f"{SESSIONS}/{sid}/message", json={"text": "second"})

    history = (await api.get(f"{SESSIONS}/{sid}/history")).json()
    assert [m["content"][0]["text"] for m in history["messages"]] == ["first", "second"]


async def test_delete_stops_container_and_terminates(api, docker_client):
    sid = (await create(api)).json()["id"]
    r = await api.delete(f"{SESSIONS}/{sid}")
    assert r.status_code == 204
    docker_client.containers.get.assert_called_with("c0ffee")
    docker_client.containers.get.return_value.remove.assert_called_once_with(force=True)
    assert (await api.get(f"{SESSIONS}/{sid}")).json()["status"] == "terminated"


async def test_unknown_session_is_404(api):
    missing = uuid.uuid4()
    assert (await api.get(f"{SESSIONS}/{missing}")).status_code == 404
    assert (await api.get(f"{SESSIONS}/{missing}/history")).status_code == 404
    assert (await api.delete(f"{SESSIONS}/{missing}")).status_code == 404


async def test_health(api):
    assert (await api.get("/health")).json() == {"status": "ok"}
