import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum as SAEnum, ForeignKey, Integer, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from database import Base


class SessionStatus(str, enum.Enum):
    CREATING = "creating"
    IDLE = "idle"
    RUNNING = "running"
    TERMINATED = "terminated"
    ERROR = "error"


class Session(Base):
    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status = Column(SAEnum(SessionStatus), nullable=False, default=SessionStatus.CREATING)
    container_id = Column(String, nullable=True)
    container_name = Column(String, nullable=True)
    container_ip = Column(String, nullable=True)
    vnc_host_port = Column(Integer, nullable=True)
    novnc_host_port = Column(Integer, nullable=True)
    model = Column(String, nullable=False, default="claude-sonnet-4-5-20250929")
    provider = Column(String, nullable=False, default="anthropic")
    tool_version = Column(String, nullable=False, default="computer_use_20251124")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = relationship(
        "Message",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String, nullable=False)
    content = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    session = relationship("Session", back_populates="messages")
