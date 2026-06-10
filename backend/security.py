"""Per-session bearer tokens.

There are no user accounts, so each session is its own capability: the
token is returned once when the session is created and only its SHA-256
hash is stored. Every session-scoped call must present it.
"""

import hashlib
import hmac
import secrets
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Session

_bearer = HTTPBearer(auto_error=False)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def token_matches(session: Session, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(session.access_token_hash, hash_token(token))


async def authorized_session(
    session_id: UUID,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Session:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing session token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    session = await db.get(Session, session_id)
    # A wrong token gets the same answer as an unknown id, so ids can't be probed.
    if session is None or not token_matches(session, credentials.credentials):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return session
