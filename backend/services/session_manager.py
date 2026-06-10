import asyncio
import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import AsyncSessionLocal
from models import Message, Session, SessionStatus, utcnow
from services.docker_manager import docker_manager

logger = logging.getLogger(__name__)


async def create_session(
    db: AsyncSession,
    api_key: str,
    model: str,
    provider: str,
    tool_version: str,
) -> Session:
    session = Session(
        id=uuid.uuid4(),
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
            model=model,
            tool_version=tool_version,
        )
        session.container_id = info["container_id"]
        session.container_name = info["container_name"]
        session.container_ip = info["container_ip"]
        session.vnc_host_port = info["vnc_host_port"]
        session.novnc_host_port = info["novnc_host_port"]
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

    return session


async def send_message_to_session(db: AsyncSession, session_id: uuid.UUID, text: str) -> Message:
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise ValueError(f"Session {session_id} not found")
    if session.status != SessionStatus.IDLE:
        raise ValueError(f"Session not idle (status: {session.status.value})")

    msg = Message(session_id=session_id, role="user", content=[{"type": "text", "text": text}])
    db.add(msg)
    session.status = SessionStatus.RUNNING
    session.updated_at = utcnow()
    await db.commit()
    await db.refresh(msg)

    asyncio.create_task(_dispatch(session.id, session.container_ip, text))
    return msg


async def _dispatch(session_id: uuid.UUID, container_ip: str, text: str):
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
            )
            resp.raise_for_status()
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


async def save_assistant_message(session_id: uuid.UUID, content: list):
    async with AsyncSessionLocal() as db:
        msg = Message(session_id=session_id, role="assistant", content=content)
        db.add(msg)
        result = await db.execute(select(Session).where(Session.id == session_id))
        session = result.scalar_one_or_none()
        if session:
            session.status = SessionStatus.IDLE
            session.updated_at = utcnow()
        await db.commit()


async def terminate_session(db: AsyncSession, session_id: uuid.UUID):
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise ValueError(f"Session {session_id} not found")

    if session.container_id:
        await docker_manager.stop_container(
            session.container_id,
            session.vnc_host_port,
            session.novnc_host_port,
        )

    session.status = SessionStatus.TERMINATED
    session.updated_at = utcnow()
    await db.commit()


async def recover_active_sessions():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Session).where(
                Session.status.in_([SessionStatus.IDLE, SessionStatus.RUNNING, SessionStatus.CREATING])
            )
        )
        sessions = result.scalars().all()
        for s in sessions:
            docker_manager.recover_ports(s.vnc_host_port, s.novnc_host_port)
        logger.info(f"Recovered {len(sessions)} active sessions from DB")
