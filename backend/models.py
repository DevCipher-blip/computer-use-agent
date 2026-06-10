import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import relationship

from database import Base


def utcnow() -> datetime:
    """Naive UTC timestamp, matching the timezone-less DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class SessionStatus(enum.StrEnum):
    CREATING = "creating"
    IDLE = "idle"
    RUNNING = "running"
    TERMINATED = "terminated"
    ERROR = "error"


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    status = Column(SAEnum(SessionStatus), nullable=False, default=SessionStatus.CREATING)
    access_token_hash = Column(String(64), nullable=False)
    container_id = Column(String, nullable=True)
    container_name = Column(String, nullable=True)
    container_ip = Column(String, nullable=True)
    vnc_host_port = Column(Integer, nullable=True)
    model = Column(String, nullable=False, default="claude-sonnet-4-5-20250929")
    provider = Column(String, nullable=False, default="anthropic")
    tool_version = Column(String, nullable=False, default="computer_use_20251124")
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    messages = relationship(
        "Message",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id = Column(Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    role = Column(String, nullable=False)
    content = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    session = relationship("Session", back_populates="messages")
