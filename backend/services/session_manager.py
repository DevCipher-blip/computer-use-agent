import asyncio
import logging
import uuid

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import AsyncSessionLocal
from models import Message, Session, SessionStatus, utcnow
from security import new_agent_token, new_session_token
from services.docker_manager import docker_manager

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (SessionStatus.CREATING, SessionStatus.IDLE, SessionStatus.RUNNING)

# Serialises the "count active sessions, then insert" check. The backend runs
# as a single uvicorn worker, so a process-local lock is enough.
_create_lock = asyncio.Lock()
# Keep references so fire-and-forget dispatch tasks aren't garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()


class SessionLimitReached(Exception):
    pass


async def create_session(
    db: AsyncSession,
    api_key: str,
    model: str,
    provider: str,
    tool_version: str,
) -> tuple[Session, str]:
    """Create the session row and its container; returns the session and its one-time token."""
    token, token_hash = new_session_token()
    async with _create_lock:
        active = await db.scalar(
            select(func.count()).select_from(Session).where(Session.status.in_(ACTIVE_STATUSES))
        )
        if active >= settings.max_concurrent_sessions:
            limit = settings.max_concurrent_sessions
            raise SessionLimitReached(f"{active} sessions are active (limit {limit}); terminate one first")
        session = Session(
            id=uuid.uuid4(),
            access_token_hash=token_hash,
            agent_token=new_agent_token(),
            status=SessionStatus.CREATING,
            model=model,
            provider=provider,
            tool_version=tool_version,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

    try:
        info = await docker_manager.spawn_session_container(
            session_id=str(session.id),
            api_key=api_key,
            agent_token=session.agent_token,
            model=model,
            tool_version=tool_version,
        )
        session.container_id = info["container_id"]
        session.container_name = info["container_name"]
        session.container_ip = info["container_ip"]
        session.vnc_host_port = info["vnc_host_port"]
        session.status = SessionStatus.IDLE
        session.updated_at = utcnow()
        await db.commit()
        await db.refresh(session)
    except Exception as e:
        session.status = SessionStatus.ERROR
        session.updated_at = utcnow()
        await db.commit()
        logger.error(f"Container spawn failed for session {session.id}: {e}")
        raise

    return session, token


async def send_message_to_session(db: AsyncSession, session: Session, text: str) -> Message:
    # Compare-and-set in one UPDATE so two simultaneous requests can't both start a run.
    claimed = await db.execute(
        update(Session)
        .where(Session.id == session.id, Session.status == SessionStatus.IDLE)
        .values(status=SessionStatus.RUNNING, updated_at=utcnow())
    )
    if claimed.rowcount != 1:
        await db.rollback()
        await db.refresh(session)
        raise ValueError(f"Session not idle (status: {session.status.value})")

    msg = Message(session_id=session.id, role="user", content=[{"type": "text", "text": text}])
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    task = asyncio.create_task(_dispatch(session.id, session.container_ip, session.agent_token, text))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return msg


def agent_headers(agent_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {agent_token}"}


async def _dispatch(session_id: uuid.UUID, container_ip: str, agent_token: str, text: str):
    ready = await wait_until_ready(container_ip)
    if not ready:
        await _set_status(session_id, SessionStatus.ERROR)
        logger.error(f"Container not ready for session {session_id}")
        return

    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(
                f"http://{container_ip}:{settings.session_agent_port}/run",
                json={"task": text, "session_id": str(session_id)},
                headers=agent_headers(agent_token),
            )
            resp.raise_for_status()
            if resp.json().get("error"):
                raise RuntimeError(resp.json()["error"])
    except Exception as e:
        logger.error(f"Dispatch failed for session {session_id}: {e}")
        await _set_status(session_id, SessionStatus.ERROR)


async def wait_until_ready(container_ip: str, timeout: int | None = None) -> bool:
    timeout = timeout or settings.container_ready_timeout
    url = f"http://{container_ip}:{settings.session_agent_port}/health"
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.5)
    return False


async def _set_status(session_id: uuid.UUID, status: SessionStatus):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Session).where(Session.id == session_id))
        session = result.scalar_one_or_none()
        if session:
            session.status = status
            session.updated_at = utcnow()
            await db.commit()


async def complete_run(db: AsyncSession, session: Session, messages: list[dict], error: str | None):
    """Record a finished task and free the session for the next one.

    Called once per task by the session agent, whether or not a browser was
    watching the stream. Repeat calls for an already-idle session are ignored.
    """
    if session.status != SessionStatus.RUNNING:
        return
    blocks = [b for m in messages if m.get("role") == "assistant" for b in m.get("content", [])]
    if error:
        blocks.append({"type": "text", "text": f"Run failed: {error}"})
    if blocks:
        db.add(Message(session_id=session.id, role="assistant", content=blocks))
    session.status = SessionStatus.IDLE
    session.updated_at = utcnow()
    await db.commit()


async def terminate_session(db: AsyncSession, session: Session):
    if session.container_id:
        await docker_manager.stop_container(session.container_id, session.vnc_host_port)

    session.status = SessionStatus.TERMINATED
    session.updated_at = utcnow()
    await db.commit()


async def recover_active_sessions():
    async with AsyncSessionLocal() as db:
        # A session still "creating" at startup lost its spawn call with the old process;
        # left alone it would count against the session limit forever.
        await db.execute(
            update(Session)
            .where(Session.status == SessionStatus.CREATING)
            .values(status=SessionStatus.ERROR, updated_at=utcnow())
        )
        await db.commit()
        sessions = (
            await db.scalars(
                select(Session).where(Session.status.in_([SessionStatus.IDLE, SessionStatus.RUNNING]))
            )
        ).all()
        for s in sessions:
            docker_manager.recover_vnc_port(s.vnc_host_port)
        logger.info(f"Recovered {len(sessions)} active sessions from DB")
