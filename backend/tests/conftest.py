import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

_DB_FILE = Path(os.environ.get("TEST_DB_DIR", "/tmp")) / "computeagent-test.sqlite3"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_FILE}"

import httpx  # noqa: E402

from database import Base, engine  # noqa: E402
from main import app  # noqa: E402
from services.docker_manager import docker_manager  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_db():
    import models  # noqa: F401  (registers tables)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture
def docker_client(monkeypatch):
    """A fake docker.DockerClient; the real one is never created in tests."""
    client = MagicMock(name="DockerClient")
    container = MagicMock(name="Container")
    container.id = "c0ffee"
    container.attrs = {"NetworkSettings": {"Networks": {"computeagent_network": {"IPAddress": "10.9.0.7"}}}}
    container.status = "running"
    client.containers.run.return_value = container
    client.containers.get.return_value = container
    monkeypatch.setattr(docker_manager, "_client", client)
    monkeypatch.setattr(docker_manager, "_used_vnc_ports", set())
    return client


@pytest.fixture
def no_dispatch(monkeypatch):
    """Record dispatches instead of calling the container's /run endpoint."""
    calls = []

    async def fake_dispatch(session_id, container_ip, agent_token, text):
        calls.append((session_id, container_ip, text))

    monkeypatch.setattr("services.session_manager._dispatch", fake_dispatch)
    return calls


@pytest.fixture
async def api(docker_client):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
