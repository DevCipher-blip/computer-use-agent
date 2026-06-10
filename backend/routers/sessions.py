from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Message, Session, SessionStatus, utcnow
from schemas import (
    CreatedSessionResponse,
    CreateSessionRequest,
    HistoryResponse,
    MessageResponse,
    SendMessageRequest,
    SessionResponse,
    VNCInfo,
)
from security import authorized_session
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


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CreatedSessionResponse)
async def create(body: CreateSessionRequest, db: AsyncSession = Depends(get_db)):
    session, token = await create_session(
        db,
        api_key=body.api_key,
        model=body.model,
        provider=body.provider,
        tool_version=body.tool_version,
    )
    return CreatedSessionResponse(**_to_response(session).model_dump(), access_token=token)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session: Session = Depends(authorized_session)):
    return _to_response(session)


@router.post("/{session_id}/message", response_model=MessageResponse, status_code=202)
async def send_message(
    body: SendMessageRequest,
    session: Session = Depends(authorized_session),
    db: AsyncSession = Depends(get_db),
):
    try:
        msg = await send_message_to_session(db, session, body.text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return MessageResponse.model_validate(msg)


@router.get("/{session_id}/history", response_model=HistoryResponse)
async def get_history(session: Session = Depends(authorized_session), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(
        select(Message).where(Message.session_id == session.id).order_by(Message.created_at)
    )
    return HistoryResponse(session_id=session.id, messages=[MessageResponse.model_validate(m) for m in rows])


@router.post("/{session_id}/complete", status_code=200)
async def complete_session(
    session: Session = Depends(authorized_session), db: AsyncSession = Depends(get_db)
):
    """Called by session_agent when the sampling loop finishes — ensures status goes back to idle."""
    if session.status == SessionStatus.RUNNING:
        session.status = SessionStatus.IDLE
        session.updated_at = utcnow()
        await db.commit()
    return {"status": "ok"}


@router.delete("/{session_id}", status_code=204)
async def delete_session(session: Session = Depends(authorized_session), db: AsyncSession = Depends(get_db)):
    await terminate_session(db, session)
