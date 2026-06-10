from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import get_db
from models import Session, SessionStatus, utcnow
from schemas import (
    CreateSessionRequest,
    HistoryResponse,
    MessageResponse,
    SendMessageRequest,
    SessionListResponse,
    SessionResponse,
    VNCInfo,
)
from services.session_manager import create_session, send_message_to_session, terminate_session

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


def _to_response(session: Session, host: str = "localhost") -> SessionResponse:
    vnc_info = None
    if session.vnc_host_port and session.novnc_host_port:
        vnc_info = VNCInfo(
            vnc_host=host,
            vnc_port=session.vnc_host_port,
            novnc_url=f"http://{host}/novnc/{session.novnc_host_port}/vnc.html?autoconnect=true&resize=scale&path=novnc/{session.novnc_host_port}/websockify",
        )
    return SessionResponse(
        id=session.id,
        status=session.status.value,
        model=session.model,
        provider=session.provider,
        tool_version=session.tool_version,
        vnc_info=vnc_info,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SessionResponse)
async def create(body: CreateSessionRequest, db: AsyncSession = Depends(get_db)):
    session = await create_session(
        db,
        api_key=body.api_key,
        model=body.model,
        provider=body.provider,
        tool_version=body.tool_version,
    )
    return _to_response(session)


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    skip: int = 0,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count(Session.id)))).scalar_one()
    rows = (
        (await db.execute(select(Session).order_by(Session.created_at.desc()).offset(skip).limit(limit)))
        .scalars()
        .all()
    )
    return SessionListResponse(sessions=[_to_response(s) for s in rows], total=total)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: UUID, db: AsyncSession = Depends(get_db)):
    session = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _to_response(session)


@router.post("/{session_id}/message", response_model=MessageResponse, status_code=202)
async def send_message(
    session_id: UUID,
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        msg = await send_message_to_session(db, session_id, body.text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return MessageResponse(
        id=msg.id,
        session_id=msg.session_id,
        role=msg.role,
        content=msg.content,
        created_at=msg.created_at,
    )


@router.get("/{session_id}/history", response_model=HistoryResponse)
async def get_history(session_id: UUID, db: AsyncSession = Depends(get_db)):
    session = (
        await db.execute(
            select(Session).options(selectinload(Session.messages)).where(Session.id == session_id)
        )
    ).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = [
        MessageResponse(
            id=m.id,
            session_id=m.session_id,
            role=m.role,
            content=m.content,
            created_at=m.created_at,
        )
        for m in session.messages
    ]
    return HistoryResponse(session_id=session_id, messages=messages)


@router.post("/{session_id}/complete", status_code=200)
async def complete_session(session_id: UUID, db: AsyncSession = Depends(get_db)):
    """Called by session_agent when the sampling loop finishes — ensures status goes back to idle."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session and session.status == SessionStatus.RUNNING:
        session.status = SessionStatus.IDLE
        session.updated_at = utcnow()
        await db.commit()
    return {"status": "ok"}


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: UUID, db: AsyncSession = Depends(get_db)):
    try:
        await terminate_session(db, session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
